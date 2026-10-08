import copy
import os
from pathlib import Path
import tempfile
import unittest

from causes import campaign_handoff as h
from causes.campaign_storage import StorageGuard


class StorageControlGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.old = self.base / 'PAUSE'
        self.global_pause = self.base / 'PAUSE_ALL'
        self.local = {'mi': str(self.base / 'MI_PAUSE'), 'texas': str(self.base / 'TEXAS_PAUSE')}
        marker = h.save_new(self.base, 'PAUSE', {'transition_id': 'fixture'})
        ownership = h.save_new(self.base, 'DRAIN.json', {'origin': 'storage_transition',
            'transition_id': 'fixture', 'pin': marker, 'stat': h.stat_identity(self.old)})
        routing = h.save_new(self.base, 'ROUTING.json', {
            'schema': 'causes.storage_pause_routing.v1', 'root_accepted': True,
            'transition_id': 'fixture', 'drain_marker': marker,
            'successor_global_pause': str(self.global_pause), 'successor_local_pauses': self.local,
            'user_pause_fanout': [str(self.old), str(self.global_pause), *self.local.values()]})
        mount = {'mount_id': '48', 'device': '8:32', 'root': '/new',
                 'target': '/campaign/new', 'fstype': 'ext4'}
        self.binding = {'schema': 'causes.prospective_storage_binding.v1',
            'backing': '/scratch/new', 'mount_target': '/campaign/new',
            'output': '/campaign/new/model', 'root_volume': '/campaign',
            'scratch_device': '2080', 'backing_inode': '100', 'output_inode': '101',
            'root_device': '2048', 'floor_bytes': 80, 'mount_identity': mount,
            'admission_control': {'schema': 'causes.storage_admission_controls.v1',
                'role': 'mi', 'pause_routing': routing, 'drain_marker': ownership}}
        self.observed = {'mount': mount, 'paths': {
            'backing': {'device': '2080', 'inode': '100'},
            'mount_target': {'device': '2080', 'inode': '100'},
            'output': {'device': '2080', 'inode': '101'},
            'root_volume': {'device': '2048', 'inode': '11'}},
            'output_free_bytes': 2000, 'root_free_bytes': 140}
        self.reports = []

    def guard(self, role='mi'):
        binding = copy.deepcopy(self.binding)
        binding['admission_control']['role'] = role
        return StorageGuard(binding, observe=lambda _: self.observed,
                            report=self.reports.append)

    def test_owned_drain_is_checked_and_observation_input_is_not_mutated(self):
        guard = self.guard()
        self.assertEqual(guard(), 140)
        self.assertEqual(guard.last_observation['admission_control']['decision']['admitting_role'], 'mi')
        self.assertNotIn('admission_control', self.observed)
        self.assertEqual(self.reports[-1]['state'], 'ready')

    def test_direct_old_pause_rewrite_fails_closed_and_reports_fault(self):
        guard = self.guard()
        self.assertEqual(guard(), 140)
        self.old.write_text('user pause')
        self.assertEqual(guard(), 0)
        self.assertIn('pin_changed', guard.last_error)
        self.assertEqual(self.reports[-1]['state'], 'guard_fault')
        self.assertIsNone(self.reports[-1]['observation'])

    def test_touching_or_removing_old_marker_cannot_be_missed(self):
        guard = self.guard()
        before = self.old.stat()
        os.utime(self.old, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(guard(), 0)
        self.assertIn('drain_marker_changed', guard.last_error)
        self.old.unlink()
        self.assertEqual(guard(), 0)
        self.assertEqual(self.reports[-1]['state'], 'guard_fault')

    def test_role_pause_is_local_and_global_pause_covers_both(self):
        mi, texas = self.guard('mi'), self.guard('texas')
        Path(self.local['mi']).write_text('pause')
        self.assertEqual(mi(), 0)
        self.assertEqual(texas(), 140)
        self.global_pause.write_text('pause')
        self.assertEqual(mi(), 0)
        self.assertEqual(texas(), 0)
        self.global_pause.unlink()
        Path(self.local['mi']).unlink()
        self.assertEqual(mi(), 140)
        self.assertEqual(texas(), 140)
        self.assertEqual(self.reports[-1]['state'], 'ready')


if __name__ == '__main__':
    unittest.main()
