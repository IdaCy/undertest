"""Synthetic real-launcher migration; saved pilot finals are never replayed."""
import copy
import fcntl
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_launch as launch
from tests.causes import test_campaign_supplement_launch as fixtures


class LauncherSuccessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LauncherTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def adoption(self, policy, name):
        ref = self.f.save(name + '.policy.json', policy)
        adopted = self.f.save(name + '.adoption.json', {'at': core.now(),
            'authorized_supplement_invocation': True, 'policy': ref,
            'phase': policy['phase'], 'max_clients': 1,
            'pilot_measurement_ids': policy['pilot_measurement_ids']})
        return ref, adopted

    def invoke(self, module, policy, name):
        return module.launch(*self.adoption(policy, name), runner=runner,
            observation=lambda: self.f.observation(policy),
            replay=self.f.replay, source_check=lambda: None)

    def retired_pilot(self):
        # A separately pinned source copy exercises the old immutable identity
        # path through the real legacy launch branch, without embedding a second
        # implementation or importing any private deployment artifact.
        path = self.f.root / 'predecessor_launcher.py'
        path.write_bytes(Path(launch.__file__).read_bytes() + b'\n# Synthetic predecessor source.\n')
        spec = importlib.util.spec_from_file_location('synthetic_predecessor_launcher', path)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        policy, _, _ = self.f.policy()
        policy['launcher_source'] = core.pin(path)
        policy['host_adapter_source'] = core.pin(Path(__file__).resolve())
        with mock.patch.object(runner, 'exchange', side_effect=self.f.exchange) as exchange:
            result = self.invoke(old, policy, 'old_pilot')
        self.assertEqual(exchange.call_count, 2)
        self.assertEqual(result['completed_ids'], policy['pilot_measurement_ids'])
        control = self.f.output / '.control'
        identity = core.pin(control / 'IDENTITY.json')
        baseline = self.f.root / 'retired_journal.jsonl'
        baseline.write_bytes((control / 'journal.jsonl').read_bytes())
        prepared = json.loads(core.pinned(policy['prepared']))
        finals = [core.pin(core.measurement_path(self.f.output, job, family='family', model='model_a') / 'FINAL.json')
                  for job in prepared['ready_jobs'][:2]]
        review = self.f.save('pilot_review.json', {'accepted_for_continuation': True,
            'prepared': policy['prepared'], 'pilot_measurement_ids': policy['pilot_measurement_ids'], 'finals': finals})
        retirement = self.f.save('retirement.json', {
            'accepted_supplement_launcher_retirement': True, 'predecessor_identity': identity,
            'prepared': policy['prepared'], 'output_root': str(self.f.output),
            'journal_cut': core.pin(baseline), 'all_claimed_terminal': True,
            'client_process_absent': True, 'executor_lock_acquired_nonblocking': True,
            'ambiguous_attempts': 0, 'evidence': [identity, core.pin(baseline), *finals]})
        expected = json.loads(core.pinned(identity))
        successor = {**copy.deepcopy(policy), 'phase': 'continue', 'pilot_acceptance': review,
                     'launcher_source': core.pin(Path(launch.__file__).resolve())}
        succession = {'accepted_supplement_launcher_succession': True,
            'predecessor_identity': identity, 'old_launcher': policy['launcher_source'],
            'new_launcher': successor['launcher_source'], 'output_root': str(self.f.output),
            **{k: expected[k] for k in ('prepared', 'core', 'runner')},
            'retirement_review': retirement, 'journal_baseline': core.pin(baseline)}
        return successor, succession, finals

    def bind_succession(self, policy, succession):
        policy['launcher_succession'] = self.f.save('succession.json', succession)
        retired = json.loads(core.pinned(succession['retirement_review']))
        for ref in [policy['launcher_succession'], *[succession[k] for k in
                    ('predecessor_identity', 'old_launcher', 'new_launcher', 'prepared', 'core',
                     'runner', 'retirement_review', 'journal_baseline')], *retired['evidence']]:
            if ref not in policy['required_pins']:
                policy['required_pins'].append(ref)

    def test_migration_keeps_identity_and_pilots_then_runs_only_unclaimed_suffix(self):
        policy, succession, finals = self.retired_pilot()
        with mock.patch.object(runner, 'exchange', side_effect=self.f.exchange) as exchange:
            with self.assertRaisesRegex(core.IntegrityError, 'output_identity_changed'):
                self.invoke(launch, policy, 'unreviewed')
            self.assertEqual(exchange.call_count, 0)
            self.bind_succession(policy, succession)
            result = self.invoke(launch, policy, 'reviewed')
            self.assertEqual(result['existing_final'], 2)
            self.assertEqual(result['new_final'], 1)
            self.assertEqual(result['completed_ids'], ['family/model_a/run2/early/oversight_strong'])
            self.assertEqual(exchange.call_count, 1)
            receipt = core.pin(self.f.output / '.control/LAUNCHER_SUCCESSION.json')
            again = self.invoke(launch, policy, 'later_explicit_adoption')
            self.assertEqual(again['existing_final'], 3)
            self.assertEqual(again['new_final'], 0)
            self.assertEqual(exchange.call_count, 1)
        core.pinned(succession['predecessor_identity'])
        core.pinned(receipt)
        for ref in finals:
            core.pinned(ref)
        self.assertEqual(json.loads(core.pinned(receipt))['succession_review'], policy['launcher_succession'])

    def test_executor_lock_precedes_migration_validation_or_receipt(self):
        policy, succession, _ = self.retired_pilot()
        self.bind_succession(policy, succession)
        with (self.f.output / '.control/executor.lock').open('ab') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(launch, 'validate_launcher_succession', wraps=launch.validate_launcher_succession) as validate:
                with self.assertRaises(BlockingIOError):
                    self.invoke(launch, policy, 'blocked')
                self.assertEqual(validate.call_count, 0)
        self.assertFalse((self.f.output / '.control/LAUNCHER_SUCCESSION.json').exists())
        core.pinned(succession['predecessor_identity'])

    def test_later_quarantine_suffix_still_holds_in_unchanged_core(self):
        policy, succession, finals = self.retired_pilot()
        self.bind_succession(policy, succession)
        log = self.f.output / '.control/journal.jsonl'
        with log.open('ab') as stream:
            stream.write(core.encoded({'event': 'route_quarantined', 'reason': 'synthetic retained hold'}) + b'\n')
        with mock.patch.object(runner, 'exchange', side_effect=AssertionError('no new request')):
            with self.assertRaisesRegex(core.AdmissionHold, 'manual_reconciliation_required'):
                self.invoke(launch, policy, 'held_suffix')
        self.assertFalse((self.f.output / 'model_a/run2').exists())
        core.pinned(succession['predecessor_identity'])
        for ref in finals:
            core.pinned(ref)


if __name__ == '__main__':
    unittest.main()
