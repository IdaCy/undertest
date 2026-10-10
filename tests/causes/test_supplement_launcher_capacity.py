"""Capacity changes retain request identity and the complete launcher history."""
import copy
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_launch as launch
from tests.causes import test_campaign_supplement_launch as fixtures
from tests.causes import test_supplement_launch_succession as succession_fixtures


class LauncherCapacityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LauncherTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def invoke(self, module, policy, name, *, cap=None):
        ref = self.f.save(name + '.policy.json', policy)
        adoption = self.f.save(name + '.adoption.json', {
            'at': core.now(), 'authorized_supplement_invocation': True,
            'policy': ref, 'phase': policy['phase'],
            'max_clients': policy['max_clients'] if cap is None else cap,
            'pilot_measurement_ids': policy['pilot_measurement_ids']})
        return module.launch(ref, adoption, runner=runner,
            observation=lambda: self.f.observation(policy),
            replay=self.f.replay, source_check=lambda: None)

    def test_two_real_workers_preserve_claim_order_and_original_requests(self):
        policy, _, _ = self.f.policy()
        policy.update(max_clients=2, host_adapter_source=core.pin(Path(__file__).resolve()))
        prepared = json.loads(core.pinned(policy['prepared']))
        barrier, lock = threading.Barrier(2), threading.Lock()
        active = peak = 0

        def exchange(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return self.f.exchange(*args)
            finally:
                with lock:
                    active -= 1

        with mock.patch.object(runner, 'exchange', side_effect=exchange) as calls:
            result = self.invoke(launch, policy, 'two_clients')
        self.assertEqual(peak, 2)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(set(result['completed_ids']), set(policy['pilot_measurement_ids']))
        self.assertIsNone(result['stop_reason'])
        rows = list(map(json.loads, (self.f.output / '.control/journal.jsonl').read_bytes().splitlines()))
        self.assertEqual([r['measurement_id'] for r in rows if r['event'] == 'claimed'],
                         policy['pilot_measurement_ids'])
        for job in prepared['ready_jobs'][:2]:
            destination = core.measurement_path(self.f.output, job, family='family', model='model_a')
            self.assertEqual(core.load(destination / 'INPUT.json'), job)
            self.assertEqual(core.pin(destination / 'capture/request.json')['sha256'], job['request_file_sha256'])
            self.assertEqual(core.load(destination / 'FINAL.json')['technical_status'], 'complete')
        intent, = (self.f.output / '.control').glob('invocation_*/INTENT.json')
        self.assertEqual(core.load(intent)['max_clients'], 2)
        self.assertFalse((self.f.output / 'model_a/run2').exists())

    def test_typed_matching_caps_are_required_before_observation_or_output(self):
        policy, _, _ = self.f.policy()
        for index, cap in enumerate((0, 3, True, 1.0)):
            with self.subTest(policy_cap=cap):
                with self.assertRaisesRegex(core.IntegrityError, 'supported_client_scope'):
                    self.invoke(launch, dict(policy, max_clients=cap), 'bad_policy_' + str(index))
        for index, cap in enumerate((1, True, 2.0)):
            with self.subTest(adoption_cap=cap):
                with self.assertRaisesRegex(core.IntegrityError, 'adoption_scope_changed'):
                    self.invoke(launch, dict(policy, max_clients=2), 'bad_adoption_' + str(index), cap=cap)
        self.assertFalse(self.f.output.exists())


class LauncherChainTests(unittest.TestCase):
    invoke = LauncherCapacityTests.invoke
    # Use the existing synthetic legacy pilot/migration fixture without copying
    # an implementation or referencing any deployment artifact.
    def setUp(self):
        self.legacy = succession_fixtures.LauncherSuccessionIntegrationTests()
        self.legacy.setUp()
        self.f = self.legacy.f
        self.addCleanup(self.legacy.doCleanups)

    def chain(self):
        first_policy, first_review, pilot_finals = self.legacy.retired_pilot()
        self.legacy.bind_succession(first_policy, first_review)
        with mock.patch.object(runner, 'exchange', side_effect=self.f.exchange):
            first = self.legacy.invoke(launch, first_policy, 'first_migration')
        self.assertEqual(first['new_final'], 1)
        control = self.f.output / '.control'
        previous = core.pin(control / 'LAUNCHER_SUCCESSION.json')
        baseline = self.f.root / 'second_retirement_journal.jsonl'
        baseline.write_bytes((control / 'journal.jsonl').read_bytes())
        prepared = json.loads(core.pinned(first_policy['prepared']))
        finals = [core.pin(core.measurement_path(self.f.output, job, family='family', model='model_a') / 'FINAL.json')
                  for job in prepared['ready_jobs']]
        path = self.f.root / 'second_launcher.py'
        path.write_bytes(Path(launch.__file__).read_bytes() + b'\n# Synthetic second source migration.\n')
        spec = importlib.util.spec_from_file_location('synthetic_second_launcher', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy = copy.deepcopy(first_policy)
        policy.update(max_clients=2, launcher_source=core.pin(path),
                      host_adapter_source=core.pin(Path(__file__).resolve()))
        retirement = self.f.save('second_retirement.json', {
            'accepted_supplement_launcher_retirement': True,
            'predecessor_identity': first_review['predecessor_identity'],
            'effective_launcher': first_policy['launcher_source'],
            'prepared': policy['prepared'], 'output_root': str(self.f.output),
            'journal_cut': core.pin(baseline), 'all_claimed_terminal': True,
            'client_process_absent': True, 'executor_lock_acquired_nonblocking': True,
            'ambiguous_attempts': 0,
            'evidence': [first_review['predecessor_identity'], previous, core.pin(baseline), *finals]})
        review = {**first_review, 'old_launcher': first_policy['launcher_source'],
                  'new_launcher': policy['launcher_source'], 'previous_succession': previous,
                  'retirement_review': retirement, 'journal_baseline': core.pin(baseline)}
        self.bind(policy, review, 'second_succession')
        return module, policy, review, first_policy, [first_review['predecessor_identity'], previous, *finals]

    def bind(self, policy, review, name):
        policy['launcher_succession'] = self.f.save(name + '.json', review)
        retirement = json.loads(core.pinned(review['retirement_review']))
        for ref in [policy['launcher_succession'], *[review[k] for k in (
                'predecessor_identity', 'old_launcher', 'new_launcher', 'prepared', 'core', 'runner',
                'retirement_review', 'journal_baseline', 'previous_succession')], *retirement['evidence']]:
            if ref not in policy['required_pins']:
                policy['required_pins'].append(ref)

    def validate(self, policy):
        prepared = json.loads(core.pinned(policy['prepared']))
        identity = {'family': prepared['family'], 'model': prepared['model'],
                    'prepared': policy['prepared'], 'core': prepared['public_source'],
                    'runner': prepared['frozen_runner'], 'launcher': policy['launcher_source']}
        return launch.validate_launcher_succession(policy, identity, self.f.output / '.control')

    def test_second_migration_is_additive_and_repeated_adoption_never_replays(self):
        module, policy, review, _, preserved = self.chain()
        with mock.patch.object(runner, 'exchange', side_effect=AssertionError('no replay')) as calls:
            for name in ('second_migration', 'same_leaf_later_adoption'):
                result = self.invoke(module, policy, name)
                self.assertEqual((result['existing_final'], result['new_final']), (3, 0))
            self.assertEqual(calls.call_count, 0)
        receipt = self.validate(policy)
        self.assertEqual(receipt['previous_succession'], review['previous_succession'])
        self.assertEqual(core.load(launch.launcher_succession_path(receipt, self.f.output / '.control')), receipt)
        for ref in preserved:
            core.pinned(ref)

    def test_disconnected_previous_receipt_and_missing_pin_are_rejected(self):
        _, policy, review, _, _ = self.chain()
        wrong = copy.deepcopy(policy)
        copied = self.f.save('copied_previous.json', json.loads(core.pinned(review['previous_succession'])))
        self.bind(wrong, dict(review, previous_succession=copied), 'copied_review')
        with self.assertRaisesRegex(core.IntegrityError, 'chain_disconnected'):
            self.validate(wrong)
        policy['required_pins'].remove(review['previous_succession'])
        with self.assertRaisesRegex(core.IntegrityError, 'previous_receipt_not_pinned'):
            self.validate(policy)

    def test_wrong_effective_source_or_shortened_retirement_prefix_is_rejected(self):
        _, policy, review, _, _ = self.chain()
        retired = json.loads(core.pinned(review['retirement_review']))
        wrong = copy.deepcopy(policy)
        changed = dict(retired, effective_launcher=review['new_launcher'])
        ref = self.f.save('wrong_effective_source.json', changed)
        self.bind(wrong, dict(review, retirement_review=ref), 'wrong_source_review')
        with self.assertRaisesRegex(core.IntegrityError, 'retired_source_changed'):
            self.validate(wrong)
        shorter = self.f.root / 'shorter_journal.jsonl'
        shorter.write_bytes(core.pinned(review['journal_baseline']).splitlines(keepends=True)[0])
        changed = dict(retired, journal_cut=core.pin(shorter))
        ref = self.f.save('shortened_retirement.json', changed)
        self.bind(policy, dict(review, retirement_review=ref, journal_baseline=core.pin(shorter)), 'shortened_review')
        with self.assertRaisesRegex(core.IntegrityError, 'journal_regressed'):
            self.validate(policy)

    def test_committed_leaf_prevents_branching_or_rollback(self):
        module, policy, review, first_policy, _ = self.chain()
        self.invoke(module, policy, 'committed_leaf')
        with self.assertRaisesRegex(core.IntegrityError, 'unbound_branch'):
            self.validate(first_policy)
        alternative = copy.deepcopy(policy)
        self.bind(alternative, dict(review, annotation='different proposed branch'), 'forked_review')
        with self.assertRaisesRegex(core.IntegrityError, 'unbound_branch'):
            self.validate(alternative)


if __name__ == '__main__':
    unittest.main()
