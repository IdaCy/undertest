import json
import os
from pathlib import Path
import tempfile
import unittest

from causes.campaign_sandbox import Sandbox, rewrite_environment


class CampaignSandboxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.spec = {'workspace': '/work/repo', 'seed_path': str(self.seed), 'actor_uid': max(1, os.getuid())}

    def box(self, output='output', **kwargs):
        return Sandbox(self.spec, self.root / output, 'test', **kwargs)

    def test_environment_relocation_preserves_unrelated_prefixes(self):
        entries = ['PATH=/work/repo/bin:/tools:/work/repository/bin', 'HOME=/tmp/home', 'GEM_HOME=/work/repo/gems']
        self.assertEqual(rewrite_environment(entries, '/work/repo', '/projects/repo'),
                         ['PATH=/projects/repo/bin:/tools:/work/repository/bin', 'HOME=/tmp/home', 'GEM_HOME=/projects/repo/gems'])

    def test_embedded_environment_path_requires_adapter(self):
        with self.assertRaises(ValueError):
            rewrite_environment(['FLAGS=--path /work/repo'], '/work/repo', '/projects/repo')

    def test_output_symlink_is_rejected_before_creating_target_children(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'alias').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.box('alias/new')
        self.assertFalse((outside / 'new').exists())

    def test_output_traversal_is_rejected_before_creating_parents(self):
        with self.assertRaises(ValueError):
            self.box('new/../other')
        self.assertFalse((self.root / 'new').exists())

    def test_workspace_cannot_overlap_runtime_masks(self):
        for path in ('/home/project', '/usr', '/tmp/project', '/work/../repo'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.box(workspace_path=path)

    def test_relative_cue_cannot_follow_symlink_or_replace_file(self):
        box = self.box()
        box.repo.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (box.repo / 'alias').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            box._write_relative('alias/new.txt', 'bad', 0o644)
        self.assertFalse((outside / 'new.txt').exists())
        original = box.repo / 'existing.txt'
        original.write_text('original')
        with self.assertRaises(FileExistsError):
            box._write_relative('existing.txt', 'replacement', 0o644)
        self.assertEqual(original.read_text(), 'original')

    def test_checkpoint_failure_always_attempts_unpause_and_records_missing_state(self):
        box = self.box()
        box.started = True
        box.record['workspace_method'] = 'overlayfs'
        tags = []

        def execute(argv, timeout=90, tag='operation', **kwargs):
            tags.append(tag)
            if tag == 'paused_state':
                return {'returncode': 0, 'stdout': json.dumps({'Paused': True, 'Pid': 10})}
            if tag.startswith('checkpoint_workspace'):
                raise OSError('snapshot failed')
            return {'returncode': 0}

        box._execute = execute
        record = box.checkpoint()
        self.assertFalse(record['complete'])
        self.assertIn('unpause', tags)
        self.assertTrue((box.root / 'checkpoints/early/CHECKPOINT.json').is_file())
        self.assertFalse(record['process_state_restoration_supported'])

    def test_unpause_failure_preserves_checkpoint_failure_record(self):
        box = self.box()
        box.started = True
        box.record['workspace_method'] = 'overlayfs'

        def execute(argv, timeout=90, tag='operation', **kwargs):
            if tag == 'unpause':
                raise OSError('daemon unavailable')
            if tag == 'paused_state':
                raise ValueError('invalid response')
            return {'returncode': 0}

        box._execute = execute
        record = box.checkpoint()
        self.assertFalse(record['complete'])
        self.assertTrue(any(item.get('stage') == 'unpause' for item in record['errors']))
        self.assertTrue((box.root / 'checkpoints/early/CHECKPOINT.json').is_file())

    def test_pause_client_timeout_still_attempts_unpause(self):
        box = self.box()
        box.started = True
        box.record['workspace_method'] = 'overlayfs'
        tags = []

        def execute(argv, timeout=90, tag='operation', **kwargs):
            tags.append(tag)
            return {'returncode': -9 if tag == 'pause' else 0, 'timeout': tag == 'pause'}

        box._execute = execute
        record = box.checkpoint()
        self.assertFalse(record['complete'])
        self.assertIn('unpause', tags)

    def test_failed_close_can_reconcile_and_retry(self):
        box = self.box()
        box.created = True
        stops = []

        def execute(argv, timeout=90, tag='operation', **kwargs):
            if tag == 'closure_state':
                return {'returncode': 0, 'stdout': json.dumps({'Running': False, 'Paused': False})}
            if tag == 'stop':
                stops.append(tag)
                return {'returncode': 1 if len(stops) == 1 else 0}
            return {'returncode': 0}

        box._execute = execute
        self.assertEqual(box.close()['state'], 'closure_requires_review')
        self.assertFalse(box.closed)
        self.assertEqual(box.close()['state'], 'closed')
        self.assertTrue(box.closed)


if __name__ == '__main__':
    unittest.main()
