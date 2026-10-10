import copy
import datetime as dt
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_storage as storage
from causes import campaign_storage_policy as policy
import test_campaign_capacity_guard as lease_tests
from test_campaign_storage import storage_fixture


class CapacityReleaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.case = lease_tests.CapacityLeaseTests('test_healthy_pending_and_lock_loss')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        binding, self.physical = storage_fixture()
        binding['mount_target'] = str(c.root)
        binding['output'] = str(c.output)
        binding['mount_identity']['target'] = str(c.root)
        self.physical['mount']['target'] = str(c.root)
        binding['capacity_guard'] = c.binding
        self.binding = binding
        launch_binding = {'fixture_exact_runtime_binding': True}
        owner_ref = h.save_new(c.root, 'OWNER.json', {'binding': launch_binding,
            'process': c.owner, 'controller_id': 'candidate'})
        self.registration = {'phase': 'mi_waiting', 'owners': {'new_mi': {
            'process': c.owner, 'controller_id': 'candidate', 'output_root': str(c.output),
            'launch_owner_receipt': owner_ref}}}
        phase = h.save_new(c.root, 'PHASE.json', self.registration)
        review = h.save_new(c.root, 'PHASE_REVIEW.json', {
            'accepted_storage_phase_registration': True, 'registration': phase})
        self.phase_pair = {'evidence': phase, 'review': review}
        self.operator = policy.StorageAdmission.__new__(policy.StorageAdmission)
        self.operator.policy = {'role': 'mi', 'output_root': str(c.output),
            'capacity_trial': {'mode': 'candidate', 'trial_id': 'fixture'},
            'identity_authorization': h.save_new(c.root, 'IDENTITY_AUTHORIZATION.json', {}),
            'launch': {'jobs': h.save_new(c.root, 'JOBS.json', []),
                'config': h.save_new(c.root, 'CONFIG.json', {}),
                'storage_binding': binding, 'record_directory': str(c.root / 'records'),
                'owned_barrier': c.binding['owned_startup_barrier'],
                'options': {'pause_file': c.binding['pause_file'],
                    'global_pause_file': str(c.root / 'GLOBAL_PAUSE'),
                    'start_barrier_file': str(c.root / 'START_BARRIER')}}}
        self.operator.policy_ref = h.save_new(c.root, 'POLICY.json', self.operator.policy)
        self.operator.readiness_pair = {'fixture': True}
        issued = dt.datetime.now(dt.timezone.utc)
        self.operator.authority_ref = h.save_new(c.root, 'AUTHORITY.json', {
            'root_authorized': True, 'no_user_revocation': True, 'action': policy.RELEASE,
            'issued_at': issued.isoformat(), 'expires_at': (issued + dt.timedelta(seconds=180)).isoformat(),
            'policy': self.operator.policy_ref, 'readiness': self.operator.readiness_pair,
            'authorized_capacity_transition': True, 'capacity_trial': self.operator.policy['capacity_trial'],
            'storage_phase_registration': self.phase_pair})
        self.observed_owners = []
        def live(*, capacity_owner=None):
            self.observed_owners.append(capacity_owner)
            checker = storage.StorageGuard(self.binding, lambda _: self.physical,
                                            capacity_owner=capacity_owner)
            storage.require(checker.check() >= checker.binding['floor_bytes'], 'storage_admission_floor')
            return {}
        self.operator.live = live
        patches = [patch.object(policy.runtime, 'launch_binding', return_value=launch_binding),
            patch.object(policy.identity, 'validate_authorization_value', return_value={}),
            patch.object(policy.identity, 'current_registration', return_value=(
                {'registration': phase, 'independent_review': review}, self.registration)),
            patch.object(policy.observation, 'observe_phase', return_value={})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_live_healthy_lease_uses_registered_owner_not_release_helper(self):
        writer = self.case.writer()
        writer.publish('healthy', self.case.sample)
        result = self.operator.release(self.phase_pair)
        self.assertTrue(result['validated'])
        self.assertFalse(result['executed'])
        self.assertEqual(self.observed_owners, [self.case.owner])
        self.assertTrue((self.case.root / 'START_BARRIER').exists())

    def test_constructor_exemption_cannot_release_without_lease(self):
        with self.assertRaisesRegex(ValueError, 'current_healthy_capacity_lease_required'):
            self.operator.release(self.phase_pair)
        self.assertEqual(self.observed_owners, [self.case.owner])
        self.assertTrue((self.case.root / 'START_BARRIER').exists())

    def test_pending_and_dead_guard_cannot_release(self):
        writer = self.case.writer()
        writer.publish('endpoint_pending', self.case.sample)
        with self.assertRaisesRegex(ValueError, 'storage_admission_floor'):
            self.operator.release(self.phase_pair)
        writer.close()
        with self.assertRaisesRegex(ValueError, 'storage_admission_floor'):
            self.operator.release(self.phase_pair)
        self.assertTrue((self.case.root / 'START_BARRIER').exists())


if __name__ == '__main__':
    unittest.main()
