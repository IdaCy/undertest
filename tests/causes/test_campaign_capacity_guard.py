import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from causes import campaign_capacity_guard as g
from causes import campaign_handoff as h
from causes import campaign_storage as storage
from test_campaign_storage import storage_fixture


class CapacityLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        marker = self.root / 'START_BARRIER'
        marker.write_bytes(b'owned\n')
        archive = self.root / 'ARCHIVED_MARKER'
        archive.write_bytes(marker.read_bytes())
        owned = h.save_new(self.root, 'OWNED.json', {
            'output_root': str(self.output), 'marker': h.pin(marker),
            'stat': h.stat_identity(marker), 'archived_marker': h.pin(archive)})
        self.owner = {'pid': 999999, 'process_starttime_ticks': '123', 'argv': ['candidate']}
        self.process = g.self_process()
        criteria = h.save_new(self.root, 'CRITERIA.json', {'fixture': True})
        criteria_review = h.save_new(self.root, 'CRITERIA_REVIEW.json', {
            'accepted_prospective_operational_criteria': True, 'criteria': criteria})
        adoption = h.save_new(self.root, 'ADOPTION.json', {
            'adopted_prospective_operational_criteria': True, 'criteria': criteria})
        reference = h.save_new(self.root, 'REFERENCE.json', {'fixture': True})
        reference_review = h.save_new(self.root, 'REFERENCE_REVIEW.json', {
            'accepted_capacity_reference': True, 'summary': reference, 'criteria': criteria})
        self.binding = {'schema': 'causes.capacity_guard_binding.v1', 'trial_id': 'fixture',
            'output_root': str(self.output), 'plan_path': str(self.root / 'PLAN.json'),
            'lease_path': str(self.root / 'health' / 'CURRENT.json'),
            'health_lock_path': str(self.root / 'health' / 'health.lock'),
            'guard_argv': [*self.process['argv'], str(self.root / 'PLAN.json')],
            'guard_source': h.pin(Path(g.__file__)), 'required_sources': [],
            'healthy_lease_seconds': 45, 'endpoint_pending_seconds': 60,
            'owned_startup_barrier': owned, 'pause_file': str(self.root / 'ROLE_PAUSE'),
            'criteria': {'evidence': criteria, 'review': criteria_review},
            'criteria_adoption': adoption,
            'reference': {'evidence': reference, 'review': reference_review}}
        self.process['argv'] = self.binding['guard_argv']
        self.plan = h.save_new(self.root, 'PLAN.json', {
            'capacity_guard': self.binding, 'owner_process': self.owner,
            **{key: self.binding[key] for key in ('criteria', 'criteria_adoption', 'reference')}})
        self.sample = h.save_new(self.root, 'SAMPLE.json', {
            'finished_at': h.now(), 'finished_monotonic_seconds': time.monotonic()})
        self.identity = patch.object(h, 'process_identity', side_effect=lambda owner: owner)
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.self_mock = patch.object(g, 'self_process', return_value=self.process)
        self.self_mock.start()
        self.addCleanup(self.self_mock.stop)

    def writer(self):
        writer = g.LeaseWriter(self.binding, self.plan, self.owner)
        self.addCleanup(writer.close)
        return writer

    def test_owned_startup_is_not_release_health(self):
        result = g.validate_capacity_guard(self.binding, self.owner)
        self.assertEqual(result['status'], 'startup_held')
        self.assertFalse(result['admission_allowed'])
        with self.assertRaises(ValueError):
            g.validate_capacity_guard(self.binding, self.owner, require_healthy=True)

    def test_missing_lease_after_barrier_blocks(self):
        (self.root / 'START_BARRIER').unlink()
        self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'blocked')

    def test_healthy_pending_and_lock_loss(self):
        writer = self.writer()
        writer.publish('healthy', self.sample)
        self.assertEqual(g.validate_capacity_guard(self.binding, self.owner, require_healthy=True)['status'], 'healthy')
        writer.publish('endpoint_pending', self.sample)
        self.assertFalse(g.validate_capacity_guard(self.binding, self.owner)['admission_allowed'])
        with self.assertRaises(ValueError):
            g.validate_capacity_guard(self.binding, self.owner, require_healthy=True)
        writer.close()
        self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'blocked')

    def test_expired_and_future_leases_refused(self):
        writer = self.writer()
        writer.publish('healthy', self.sample)
        with patch.object(g.time, 'monotonic', return_value=time.monotonic() + 50):
            self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'blocked')
        with patch.object(g.time, 'monotonic', return_value=0):
            self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'blocked')

    def test_valid_but_expired_lease_is_distinct_from_malformed(self):
        sample = h.save_new(self.root, 'OLD_SAMPLE.json', {
            'finished_at': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=50)).isoformat(),
            'finished_monotonic_seconds': time.monotonic() - 50})
        writer = self.writer()
        writer.publish('healthy', sample)
        self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'expired')
        with self.assertRaises(ValueError):
            g.validate_capacity_guard(self.binding, self.owner, require_healthy=True)

    def test_wrong_reference_in_plan_never_qualifies(self):
        value = h.checked(self.plan)
        value['reference'] = value['criteria']
        Path(self.plan['path']).write_bytes(h.encoded(value))
        with self.assertRaisesRegex(ValueError, 'evidence_changed'):
            g.LeaseWriter(self.binding, h.pin(self.plan['path']), self.owner)

    def test_exact_constructed_duration_survives_float_exponent_boundary(self):
        self.assertNotEqual((1000.1 + 60) - 1000.1, 60)
        sample = h.save_new(self.root, 'EXPONENT_SAMPLE.json', {
            'finished_at': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),
            'finished_monotonic_seconds': 1000.1})
        writer = self.writer()
        writer.publish('endpoint_pending', sample)
        with patch.object(g.time, 'monotonic', return_value=1001.1):
            self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'endpoint_pending')

    def test_candidate_or_plan_drift_refused(self):
        writer = self.writer()
        writer.publish('healthy', self.sample)
        changed = dict(self.owner, pid=999998)
        self.assertEqual(g.validate_capacity_guard(self.binding, changed)['status'], 'blocked')
        Path(self.plan['path']).write_text('{}')
        self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'blocked')

    def test_failed_durable_write_revokes_lock(self):
        writer = self.writer()
        writer.publish('healthy', self.sample)
        with patch.object(h, 'save_new', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                writer.publish('healthy', self.sample)
        self.assertTrue(writer.lock.closed)
        self.assertEqual(g.validate_capacity_guard(self.binding, self.owner)['status'], 'blocked')
        with self.assertRaises(ValueError):
            writer.publish('healthy', self.sample)

    def test_hold_never_reinterprets_existing_pause(self):
        path = Path(self.binding['pause_file'])
        path.write_bytes(b'user-owned\n')
        result = h.checked(g.hold_admission(self.binding, str(path), self.root, ['fixture']))
        self.assertFalse(result['created'])
        self.assertEqual(path.read_bytes(), b'user-owned\n')


class PerClaimHookTests(unittest.TestCase):
    def test_startup_exemption_never_permits_a_claim(self):
        binding, observation = storage_fixture()
        binding['capacity_guard'] = {'output_root': binding['output']}
        with patch.object(storage, 'validate_capacity_guard', return_value={
                'status': 'startup_held', 'admission_allowed': False, 'construction_allowed': True}):
            instance = storage.StorageGuard(binding, lambda _: observation)
            self.assertEqual(instance.check(), 140)
            self.assertEqual(instance(), 0)
            self.assertEqual(instance.last_observation['root_free_bytes'], 140)

    def test_absent_optional_field_preserves_legacy_behavior(self):
        binding, observation = storage_fixture()
        with patch.object(storage, 'validate_capacity_guard', side_effect=AssertionError('must not call')):
            instance = storage.StorageGuard(binding, lambda _: observation)
            self.assertEqual(instance(), 140)


if __name__ == '__main__':
    unittest.main()
