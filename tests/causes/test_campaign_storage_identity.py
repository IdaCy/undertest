import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_storage_identity as original


class StorageIdentityBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.bundle = self.base / 'bundle'
        self.package = self.bundle / 'causes'
        self.package.mkdir(parents=True)
        self.serial = 0
        source = Path(original.__file__).read_bytes()
        module_path = self.package / 'campaign_storage_identity.py'
        module_path.write_bytes(source)
        (self.package / '__init__.py').write_text('')
        for name in ('campaign_storage.py', 'campaign_dispatcher.py', 'campaign_handoff.py'):
            (self.package / name).write_text('')
        (self.package / 'campaign_storage_observation.py').write_text(
            "def observe_phase(registration):\n"
            "    assert registration['phase'] == 'texas_running_mi_retired'\n"
            "    return {'schema': 'fixture.phase', 'phase': registration['phase'],\n"
            "            'runtime_actions': False, 'fixture_only': True}\n")
        spec = importlib.util.spec_from_file_location('fixture_storage_bridge', module_path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        outputs = {role: str(self.base / 'runs' / role / ('mi27' if role.endswith('_mi') else 'tex9'))
                   for role in ('old_mi', 'old_texas', 'new_mi', 'new_texas')}
        self.manifest = {'schema': 'causes.storage_identity_manifest.v1',
            'campaign_root': str(self.base), 'pythonpath': str(self.bundle),
            'pointer': str(self.base / 'CURRENT.json'), 'outputs': outputs,
            'python': self.pin(Path(sys.executable).resolve()),
            'source_inventory': {p.name: self.pin(p) for p in self.package.iterdir() if p.suffix == '.py'}}
        self.manifest_ref = self.save(self.manifest)
        self.auth = {'schema': 'causes.storage_identity_authorization.v1',
            'overlay_module': self.pin(module_path), 'manifest': self.manifest_ref,
            'manifest_review': self.save({'accepted_storage_identity_manifest': True,
                                         'manifest': self.manifest_ref})}
        self.registration = {'schema': 'causes.storage_controller_phase.v1',
            'identity_manifest': self.manifest_ref, 'phase': 'texas_running_mi_retired',
            'owners': {}}
        for index, role in enumerate(('old_mi', 'old_texas', 'new_texas'), 1):
            owner = {'output_root': outputs[role], 'process': {'pid': index,
                'process_starttime_ticks': str(index * 100), 'argv': ['fixture', role]},
                'controller_id': role, 'observation_state': 'running' if role == 'new_texas' else 'retired'}
            if role == 'new_texas':
                guard = {'binding': {'output': outputs[role]}, 'directory': str(self.base / 'guard'),
                         'reporting_lock': {'inode': 'fixture'}}
                owner['guard'] = guard
                owner['launch_owner_receipt'] = self.save({'process': owner['process'],
                    'controller_id': role, 'binding': {'output': outputs[role],
                    'storage_binding': guard['binding']}, 'guard_directory': guard['directory'],
                    'guard_reporting_lock': guard['reporting_lock']})
            self.registration['owners'][role] = owner
        self.publish(self.registration)

    def pin(self, path):
        raw = Path(path).read_bytes()
        return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def save(self, value):
        self.serial += 1
        path = self.base / (str(self.serial) + '.json')
        path.write_text(json.dumps(value))
        return self.pin(path)

    def publish(self, registration, accepted=True):
        ref = self.save(registration)
        review = self.save({'accepted_storage_phase_registration': accepted, 'registration': ref})
        adoption = self.save({'root_accepted': True, 'registration': ref,
                             'independent_review': review, 'manifest': self.manifest_ref})
        self.pointer = {'schema': 'causes.storage_identity_pointer.v1',
            'manifest': self.manifest_ref, 'registration': ref,
            'independent_review': review, 'root_adoption': adoption}
        Path(self.manifest['pointer']).write_text(json.dumps(self.pointer))

    def test_real_isolated_worker_does_not_use_parent_causes_imports(self):
        # The child uses a pinned observation fixture; real mount/process tests live
        # in test_campaign_storage_observation.py and are not repeated here.
        value = self.module.resolve_controllers(self.auth)
        self.assertEqual([r['role'] for r in value['controllers']], ['new_texas'])
        self.assertTrue(value['succession_observations'][0]['fixture_only'])
        self.assertFalse(value['runtime_actions'])
        self.assertEqual(value['identity_receipt'], self.pointer['registration'])

    def test_changed_pinned_source_is_rejected_before_worker(self):
        with (self.package / 'campaign_storage_observation.py').open('a') as f:
            f.write('\nchanged = True\n')
        with patch.object(self.module.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                self.module.resolve_controllers(self.auth)
            run.assert_not_called()

    def test_unaccepted_phase_or_missing_root_adoption_cannot_resolve(self):
        self.publish(self.registration, accepted=False)
        with self.assertRaisesRegex(ValueError, 'not_independently_accepted'):
            self.module.resolve_controllers(self.auth)
        self.publish(self.registration)
        self.pointer['root_adoption'] = self.save({'root_accepted': False})
        Path(self.manifest['pointer']).write_text(json.dumps(self.pointer))
        with self.assertRaisesRegex(ValueError, 'not_adopted'):
            self.module.resolve_controllers(self.auth)

    def test_launch_receipt_must_bind_exact_guard_and_owner(self):
        changed = copy.deepcopy(self.registration)
        changed['owners']['new_texas']['guard']['reporting_lock'] = {'inode': 'other'}
        self.publish(changed)
        with self.assertRaisesRegex(ValueError, 'launch_provenance_changed'):
            self.module.resolve_controllers(self.auth)

    def test_module_path_and_minimum_inventory_are_explicit(self):
        altered = copy.deepcopy(self.auth)
        altered['overlay_module']['path'] = str(self.base / 'unrelated.py')
        with self.assertRaisesRegex(ValueError, 'loaded_storage_identity_source_changed'):
            self.module.validate_authorization_value(altered)
        manifest = copy.deepcopy(self.manifest)
        del manifest['source_inventory']['campaign_handoff.py']
        altered = copy.deepcopy(self.auth)
        altered['manifest'] = self.save(manifest)
        altered['manifest_review'] = self.save({'accepted_storage_identity_manifest': True,
                                                'manifest': altered['manifest']})
        with self.assertRaisesRegex(ValueError, 'inventory_required'):
            self.module.validate_authorization_value(altered)

    def test_worker_failure_never_returns_healthy_resolution(self):
        self.registration['phase'] = 'invalid_fixture_phase'
        self.publish(self.registration)
        with self.assertRaisesRegex(ValueError, 'worker_failed'):
            self.module.resolve_controllers(self.auth)


if __name__ == '__main__':
    unittest.main()
