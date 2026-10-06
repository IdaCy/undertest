import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes.task_bank import load_task


class TaskBankTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.task = self.root / 'task.json'
        self.value = {
            'schema': 'causes.portable_task.v1', 'task_id': 'fixture',
            'spec': {'task_id': 'fixture', 'request': 'Synthetic fixture.', 'image': 'sha256:fixture'},
            'assets': {'task_root': 'tasks/fixture', 'seed_path': 'seeds/fixture', 'module_path': None},
        }
        for name in ('tasks/fixture', 'seeds/fixture', 'modules/fixture'):
            (self.assets / name).mkdir(parents=True)

    def write(self):
        self.task.write_text(json.dumps(self.value))

    def test_resolves_paths_without_changing_fields_or_files(self):
        self.write()
        raw = self.task.read_bytes()
        result = load_task(self.task, self.assets, expected_sha256=hashlib.sha256(raw).hexdigest())
        for key, value in self.value['spec'].items():
            self.assertEqual(result[key], value)
        self.assertEqual(result['seed_path'], str(self.assets / 'seeds/fixture'))
        self.assertIsNone(result['module_path'])
        self.assertEqual(self.task.read_bytes(), raw)
        self.value['assets']['module_path'] = 'modules/fixture'
        self.write()
        self.assertEqual(load_task(self.task, self.assets)['module_path'], str(self.assets / 'modules/fixture'))

    def test_absolute_traversal_and_noncanonical_paths_are_rejected(self):
        for path in ('/tmp', '../elsewhere', 'seeds/../fixture', './seeds/fixture',
                     'seeds//fixture', 'seeds\\fixture', ''):
            with self.subTest(path=path):
                self.value['assets']['seed_path'] = path
                self.write()
                with self.assertRaises(ValueError):
                    load_task(self.task, self.assets)

    def test_symlink_assets_and_aliased_roots_are_rejected(self):
        (self.assets / 'alias').symlink_to(self.assets / 'seeds', target_is_directory=True)
        self.value['assets']['seed_path'] = 'alias/fixture'
        self.write()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            load_task(self.task, self.assets)
        self.value['assets']['seed_path'] = 'seeds/fixture'
        self.write()
        alias = self.root / 'aliased-assets'
        alias.symlink_to(self.assets, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'canonical'):
            load_task(self.task, alias)

    def test_missing_directory_never_downloads_or_creates(self):
        self.value['assets']['module_path'] = 'missing/module'
        self.write()
        with self.assertRaises(FileNotFoundError):
            load_task(self.task, self.assets)
        self.assertFalse((self.assets / 'missing').exists())

    def test_pins_identity_and_conflicting_execution_paths_are_rejected(self):
        self.write()
        with self.assertRaisesRegex(ValueError, 'pin_mismatch'):
            load_task(self.task, self.assets, expected_sha256='0' * 64)
        self.value['spec']['task_id'] = 'other'
        self.write()
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
            load_task(self.task, self.assets)
        self.value['spec']['task_id'] = 'fixture'
        self.value['spec']['seed_path'] = '/old/execution/path'
        self.write()
        with self.assertRaisesRegex(ValueError, 'asset_fields_mismatch'):
            load_task(self.task, self.assets)


if __name__ == '__main__':
    unittest.main()
