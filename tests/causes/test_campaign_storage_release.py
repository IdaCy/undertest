import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_storage_policy as policy
from causes import campaign_storage_release as release
import test_campaign_capacity_release as capacity_tests


class QualifiedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capacity_tests.CapacityReleaseIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.case.root
        self.serial = 0
        self.barrier = self.root / 'START_BARRIER'
        self.records = self.root / 'qualified-release'
        (self.root / 'records').mkdir()
        self.fixture.registration['owners']['new_mi']['observation_state'] = 'waiting'
        self.registration = self.fixture.registration
        self.phase = self.save(self.registration)
        review = self.save({'accepted_storage_phase_registration': True,
                            'registration': self.phase})
        self.pair = {'evidence': self.phase, 'review': review}
        self.adoption = self.save({'fixture': 'phase adoption'})
        self.pointer = {'registration': self.phase, 'independent_review': review,
                        'root_adoption': self.adoption}
        self.now = dt.datetime.now(dt.timezone.utc)
        self.evidence = {'schema': 'causes.storage_phase_consumers.v1',
            'accepted_storage_phase_consumers': True,
            'storage_registration': self.phase, 'phase_adoption': self.adoption,
            'observed_from': (self.now - dt.timedelta(seconds=2)).isoformat(),
            'observed_until': (self.now - dt.timedelta(seconds=1)).isoformat()}
        for name in ('status_config', 'sync_config', 'status_result', 'sync_result'):
            self.evidence[name] = self.save({'fixture': name})
        for name in ('status_execution', 'sync_execution'):
            self.evidence[name] = self.save({'returncode': 0, 'timeout': False})
        self.plan = {'schema': 'causes.storage_consumer_qualified_release_plan.v1',
            'source': h.pin(Path(release.__file__).resolve()), 'role': 'mi',
            'policy': self.fixture.operator.policy_ref,
            'readiness': self.fixture.operator.readiness_pair,
            'root_adoption': self.save({'fixture': 'release adoption'}),
            'phase': self.pair, 'phase_adoption': self.adoption,
            'receipt_directory': str(self.records)}
        self.writer = self.fixture.case.writer()
        self.writer.publish('healthy', self.fixture.case.sample)
        self.live = self.mock(policy.StorageAdmission, 'live',
                              side_effect=self.fixture.operator.live)
        self.mock(policy.identity, 'current_registration',
                  side_effect=lambda *_: (self.pointer, self.registration))
        case = self
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return case.now
        self.mock(release, 'dt', new=SimpleNamespace(datetime=Clock, timezone=dt.timezone))

    def mock(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def save(self, value):
        self.serial += 1
        return h.save_new(self.root, f'qualified-input-{self.serial}.json', value)

    def inputs(self, authority_delta=None):
        self.evidence['published_identity'] = self.save(self.pointer)
        evidence = self.save(self.evidence)
        pair = {'evidence': evidence, 'review': self.save({
            'accepted_storage_phase_consumers': True, 'evidence': evidence})}
        plan = self.save({**self.plan, 'consumers': pair})
        authority = {**h.checked(self.fixture.operator.authority_ref),
            'storage_phase_registration': self.pair,
            'release_preparation_review': self.save({
                'accepted_storage_release_preparation': True, 'plan': plan}),
            'release_plan': plan, 'root_adoption': self.plan['root_adoption'],
            'consumers': pair}
        authority.update(authority_delta or {})
        return plan, self.save(authority)

    def invoke(self, **authority_delta):
        return release.release(*self.inputs(authority_delta))

    def assert_preflight_refused(self, message, **authority_delta):
        with self.assertRaisesRegex(ValueError, message):
            self.invoke(**authority_delta)
        self.assertTrue(self.barrier.exists())
        self.assertFalse(self.records.exists())

    def test_exact_authority_releases_only_owned_barrier_with_registered_owner(self):
        retained = self.root / 'HISTORICAL_PAUSE'
        retained.write_bytes(b'preserved')
        result = self.invoke()
        outer = h.checked(result['result'])
        inner = h.checked(outer['release'])
        self.assertTrue(result['passed'])
        self.assertFalse(self.barrier.exists())
        self.assertEqual(retained.read_bytes(), b'preserved')
        self.assertEqual(inner['released_owned_barrier'],
                         self.fixture.case.binding['owned_startup_barrier'])
        self.assertTrue(outer['actual_running_observation_pending'])
        self.assertFalse(outer['automatic_retry'])
        self.assertGreater(len(self.fixture.observed_owners), 1)
        self.assertTrue(all(owner == self.fixture.case.owner
                            for owner in self.fixture.observed_owners))

    def test_capacity_authority_requires_exact_adopted_payload_before_live(self):
        for delta in ({'authorized_capacity_transition': False},
                      {'capacity_trial': {'mode': 'candidate', 'trial_id': 'other'}}):
            with self.subTest(delta=delta):
                self.assert_preflight_refused('capacity_runtime_authority_required', **delta)
        self.live.assert_not_called()

    def test_wrong_release_plan_cannot_borrow_accepted_consumer_pair(self):
        self.assert_preflight_refused('qualified_release_authority_changed',
                                      release_plan=self.phase)

    def test_unaccepted_or_nonwaiting_owner_rejected_before_live(self):
        self.plan['phase'] = {'evidence': self.phase, 'review': self.save({
            'accepted_storage_phase_registration': False, 'registration': self.phase})}
        self.assert_preflight_refused('unaccepted_storage_input')
        changed = copy.deepcopy(self.registration)
        changed['owners']['new_mi']['observation_state'] = 'running'
        phase = self.save(changed)
        self.plan['phase'] = {'evidence': phase, 'review': self.save({
            'accepted_storage_phase_registration': True, 'registration': phase})}
        self.assert_preflight_refused('requires_accepted_waiting_owner')
        self.live.assert_not_called()

    def test_consumer_pointer_phase_mismatch_rejected(self):
        self.pointer['independent_review'] = self.adoption
        self.assert_preflight_refused('qualified_release_consumer_phase_changed')

    def test_consumer_freshness_exact_180_seconds_is_allowed(self):
        self.evidence['observed_from'] = (self.now - dt.timedelta(seconds=181)).isoformat()
        self.evidence['observed_until'] = (self.now - dt.timedelta(seconds=180)).isoformat()
        self.assertTrue(self.invoke()['passed'])

    def test_stale_future_or_reversed_consumer_interval_rejected(self):
        for start, end in ((-182, -180.000001), (-1, 1), (0, -1)):
            with self.subTest(start=start, end=end):
                self.evidence['observed_from'] = (self.now + dt.timedelta(seconds=start)).isoformat()
                self.evidence['observed_until'] = (self.now + dt.timedelta(seconds=end)).isoformat()
                self.assert_preflight_refused('qualified_release_consumers_not_fresh')

    def test_failed_or_timed_out_consumer_never_releases(self):
        for name in ('status_execution', 'sync_execution'):
            for execution in ({'returncode': 1}, {'returncode': 0, 'timeout': True}):
                with self.subTest(name=name, execution=execution):
                    self.evidence[name] = self.save(execution)
                    self.assert_preflight_refused('qualified_release_consumer_execution_failed')
            self.evidence[name] = self.save({'returncode': 0})

    def test_changed_opaque_result_pin_rejected_before_receipts(self):
        Path(self.evidence['sync_result']['path']).write_bytes(b'changed')
        self.assert_preflight_refused('handoff_input_pin_changed')

    def test_phase_authority_mismatch_retains_uncertainty_without_unlink(self):
        with self.assertRaisesRegex(ValueError, 'storage_release_phase_authority_changed'):
            self.invoke(storage_phase_registration={'evidence': self.adoption})
        self.assertTrue(self.barrier.exists())
        failed = json.loads((self.records / 'FAILED.json').read_text())
        self.assertTrue(failed['barrier_may_have_been_released'])
        self.assertTrue(failed['manual_reconciliation_required'])
        self.assertFalse(failed['automatic_retry'])
        self.assertFalse((self.records / 'RESULT.json').exists())

    def test_consumer_expiring_during_phase_check_is_rechecked_before_unlink(self):
        def elapsed(_):
            self.now += dt.timedelta(seconds=181)
        with patch.object(policy.observation, 'observe_phase', side_effect=elapsed):
            with self.assertRaisesRegex(ValueError, 'qualified_release_consumers_not_fresh'):
                self.invoke()
        self.assertTrue(self.barrier.exists())
        self.assertTrue((self.records / 'FAILED.json').exists())
        self.assertFalse((self.root / 'records/release').exists())

    def test_capacity_lease_lost_during_phase_observation_blocks_release(self):
        with patch.object(policy.observation, 'observe_phase',
                          side_effect=lambda _: self.writer.close()):
            with self.assertRaisesRegex(ValueError, 'current_healthy_capacity_lease_required'):
                self.invoke()
        self.assertTrue(self.barrier.exists())
        self.assertTrue((self.records / 'FAILED.json').exists())

    def test_failure_after_unlink_preserves_inner_success_and_outer_uncertainty(self):
        real_save = h.save_new
        def fail_final(directory, name, value):
            if Path(directory) == self.records and name == 'RESULT.json':
                raise OSError('fixture final receipt failure')
            return real_save(directory, name, value)
        with patch.object(h, 'save_new', side_effect=fail_final):
            with self.assertRaisesRegex(OSError, 'fixture final receipt failure'):
                self.invoke()
        self.assertFalse(self.barrier.exists())
        self.assertTrue((self.root / 'records/release/RESULT.json').exists())
        failed = json.loads((self.records / 'FAILED.json').read_text())
        self.assertTrue(failed['barrier_may_have_been_released'])
        self.assertFalse(failed['automatic_retry'])
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(json.loads((self.records / 'FAILED.json').read_text()), failed)


if __name__ == '__main__':
    unittest.main()
