import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes.campaign_snapshot import (
    REMOTE_CODE, SnapshotConfig, synchronize_completed, validate_manifest,
)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.remote = self.base / 'remote'
        self.roots = tuple(f'runs/{name}/model' for name in ('trial4', 'trial8', 'main', 'continuous'))
        self.config = SnapshotConfig('fixture_host', str(self.remote), self.roots,
                                     self.base / 'local', free_space_floor_bytes=0)
        self.real_run = subprocess.run
        self.transport_calls = []

    def add_run(self, name='one', root=None, closed=True):
        run = self.remote / (root or self.roots[-1]) / name
        run.mkdir(parents=True)
        (run / 'summary.json').write_text(json.dumps({
            'sandbox_closure': {'state': 'closed' if closed else 'open', 'overlay_mounted': not closed},
            'finished_at': '2000-01-01T00:00:00+00:00',
        }))
        (run / 'evidence.bin').write_bytes(b'original evidence\x00\xff')
        return run

    def authorize(self, config):
        return dict(host=config.host, remote_base=config.remote_base, roots=list(config.roots),
                    evidence={'registration_sha256': 'a' * 64})

    def manifest(self, accepted=()):
        request = dict(host=self.config.host, base=self.config.remote_base,
                       roots=list(self.roots), accepted=list(accepted), limit=self.config.limit)
        result = self.real_run([sys.executable, '-c', REMOTE_CODE], input=json.dumps(request),
                               capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def transport(self, command, **kwargs):
        self.transport_calls.append(command[0])
        if command[0] == 'ssh':
            return self.real_run([sys.executable, '-c', REMOTE_CODE], input=kwargs['input'],
                                 capture_output=True, text=True)
        self.assertEqual(command[0], 'rsync')
        self.assertIn('--no-links', command)
        listing = next(item.split('=', 1)[1] for item in command if item.startswith('--files-from='))
        destination = Path(command[-1])
        for value in Path(listing).read_bytes().split(b'\x00'):
            if value:
                relative = value.decode()
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.remote / relative, target)
        return subprocess.CompletedProcess(command, 0)

    def test_remote_manifest_requires_closure_and_preserves_evidence_without_workspace_trees(self):
        run = self.add_run()
        self.add_run('active', closed=False)
        workspace = run / 'sandbox/sandbox/upper'
        workspace.mkdir(parents=True)
        (workspace / 'uncommitted.txt').write_text('workspace')
        (run / 'linked').symlink_to(run / 'evidence.bin')
        (run / 'checkpoint.tar').write_bytes(b'opaque checkpoint')
        manifest = self.manifest()
        validate_manifest(manifest, self.config, set())
        self.assertEqual([row['run'] for row in manifest['runs']], [self.roots[-1] + '/one'])
        self.assertEqual({item['path'] for item in manifest['runs'][0]['files']},
                         {'summary.json', 'evidence.bin', 'checkpoint.tar'})
        self.assertIn({'run': self.roots[-1] + '/active', 'reason': 'sandbox_not_confirmed_closed'}, manifest['skipped'])
        self.assertEqual(self.manifest([self.roots[-1] + '/one'])['runs'], [])

    def test_success_preserves_old_index_bytes_and_copies_each_of_four_roots_once(self):
        for root in self.roots:
            self.add_run(root=root)
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport):
            first = synchronize_completed(self.config, self.authorize)
            index = self.config.destination / 'accepted.jsonl'
            before = index.read_bytes()
            second = synchronize_completed(self.config, self.authorize)
        self.assertEqual(first['verified_runs'], 4)
        self.assertEqual(second['verified_runs'], 0)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.transport_calls, ['ssh', 'rsync', 'ssh'])
        for line in before.splitlines():
            receipt = json.loads(line)
            self.assertEqual(Path(receipt['path'], 'evidence.bin').read_bytes(), b'original evidence\x00\xff')
            self.assertEqual(hashlib.sha256(Path(receipt['manifest']).read_bytes()).hexdigest(), receipt['manifest_sha256'])

    def test_corrupt_copy_retains_attempt_without_index_acceptance(self):
        self.add_run()
        def corrupt(command, **kwargs):
            result = self.transport(command, **kwargs)
            if command[0] == 'rsync':
                (Path(command[-1]) / self.roots[-1] / 'one/evidence.bin').write_bytes(b'corrupt')
            return result
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=corrupt), self.assertRaisesRegex(RuntimeError, 'snapshot verification failed'):
            synchronize_completed(self.config, self.authorize)
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())
        attempts = list((self.config.destination / 'attempts').iterdir())
        self.assertEqual(len(attempts), 1)
        self.assertTrue((attempts[0] / 'MANIFEST.json').is_file())
        self.assertTrue((attempts[0] / 'data').is_dir())

    def test_changed_authorization_cannot_accept_copied_bytes(self):
        self.add_run()
        calls = 0
        def changing(config):
            nonlocal calls
            calls += 1
            evidence = self.authorize(config)
            evidence['evidence']['registration_sha256'] = str(calls) * 64
            return evidence
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport), self.assertRaisesRegex(RuntimeError, 'authorization_changed'):
            synchronize_completed(self.config, changing)
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())
        self.assertEqual(calls, 2)

    def test_disk_floor_prevents_copy_and_preserves_remote_manifest(self):
        self.add_run()
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport), \
             patch('causes.campaign_snapshot.shutil.disk_usage', return_value=SimpleNamespace(free=0)), \
             self.assertRaisesRegex(RuntimeError, 'free-space floor'):
            synchronize_completed(self.config, self.authorize)
        self.assertEqual(self.transport_calls, ['ssh'])
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())
        self.assertEqual(len(list((self.config.destination / 'attempts').glob('*/MANIFEST.json'))), 1)

    def test_unterminated_index_is_preserved_and_refused_before_transport(self):
        self.config.destination.mkdir()
        index = self.config.destination / 'accepted.jsonl'
        original = json.dumps({'run': self.roots[0] + '/retained'}).encode()
        index.write_bytes(original)
        with patch('causes.campaign_snapshot.subprocess.run') as run, self.assertRaisesRegex(ValueError, 'unterminated_snapshot_index'):
            synchronize_completed(self.config, self.authorize)
        run.assert_not_called()
        self.assertEqual(index.read_bytes(), original)
        self.assertFalse((self.config.destination / 'attempts').exists())

    def test_authorization_numeric_type_changes_cannot_accept_copied_bytes(self):
        self.add_run()
        calls = 0
        def changing(config):
            nonlocal calls
            calls += 1
            evidence = self.authorize(config)
            evidence['evidence']['revision'] = 1 if calls == 1 else 1.0
            return evidence
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport), self.assertRaisesRegex(RuntimeError, 'authorization_changed'):
            synchronize_completed(self.config, changing)
        self.assertEqual(calls, 2)
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())

    def test_unauthorized_scope_and_symlink_paths_refuse_before_transport(self):
        outside = self.base / 'outside'
        outside.mkdir()
        alias = self.base / 'alias'
        alias.symlink_to(outside, target_is_directory=True)
        invalid = [replace(self.config, host='-oProxyCommand=x'),
                   replace(self.config, roots=('runs/../escape',)),
                   replace(self.config, destination=alias / 'snapshot')]
        with patch('causes.campaign_snapshot.subprocess.run') as run:
            for config in invalid:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    synchronize_completed(config, self.authorize)
            with self.assertRaisesRegex(ValueError, 'authorization_scope'):
                synchronize_completed(self.config, lambda config: {'host': config.host})
            run.assert_not_called()

    def test_remote_manifest_cannot_add_out_of_scope_or_duplicate_files(self):
        self.add_run()
        original = self.manifest()
        def alter_run(manifest):
            manifest['runs'][0]['run'] = 'runs/other/model/one'
        def duplicate_file(manifest):
            manifest['runs'][0]['files'].append(copy.deepcopy(manifest['runs'][0]['files'][0]))
        def escape_file(manifest):
            manifest['runs'][0]['files'][0]['path'] = '../outside'
        def wrong_summary(manifest):
            manifest['runs'][0]['summary_sha256'] = '0' * 64
        for mutate in (alter_run, duplicate_file, escape_file, wrong_summary):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                validate_manifest(manifest, self.config, set())


if __name__ == '__main__':
    unittest.main()
