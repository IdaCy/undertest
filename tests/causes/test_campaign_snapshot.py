import copy
from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
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
    REMOTE_CODE, SnapshotConfig, SnapshotSpaceHold, synchronize_completed, validate_manifest,
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
        value = dict(host=config.host, remote_base=config.remote_base, roots=list(config.roots),
                     evidence={'registration_sha256': 'a' * 64})
        if config.selected_runs is not None:
            value['selected_runs'] = list(config.selected_runs)
        return value

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

    def local_manifest(self, accepted=(), limit=128, selected_runs=None):
        request = dict(host=self.config.host, base=self.config.remote_base,
                       roots=list(self.roots), accepted=list(accepted), limit=limit)
        if selected_runs is not None:
            request['selected_runs'] = list(selected_runs)
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(request))), redirect_stdout(output):
            exec(compile(REMOTE_CODE, '<snapshot manifest>', 'exec'), {})
        return json.loads(output.getvalue())

    def test_accepted_entries_are_not_probed_even_if_metadata_would_fail(self):
        directory = self.add_run('accepted_directory')
        regular = directory.parent / 'accepted_file'
        regular.write_text('not a run')
        link = directory.parent / 'accepted_link'
        link.symlink_to(directory, target_is_directory=True)
        dangling = directory.parent / 'accepted_dangling'
        dangling.symlink_to(directory.parent / 'missing')
        accepted = {directory, regular, link, dangling}
        self.add_run('new_run')
        original_link, original_dir = Path.is_symlink, Path.is_dir
        calls = []
        def check(method):
            def checked(path):
                if path in accepted:
                    calls.append((method.__name__, str(path)))
                    raise OSError('accepted metadata must not be accessed')
                return method(path)
            return checked
        with patch.object(Path, 'is_symlink', check(original_link)), \
             patch.object(Path, 'is_dir', check(original_dir)):
            result = self.local_manifest(str(path.relative_to(self.remote)) for path in accepted)
        self.assertEqual(calls, [])
        self.assertEqual([row['run'] for row in result['runs']], [self.roots[-1] + '/new_run'])
        self.assertEqual(result['skipped'], [
            {'root': root, 'reason': 'root_not_present'} for root in self.roots[:-1]])

    def test_manifest_keeps_root_and_lexical_order_cap_and_unsafe_entry_checks(self):
        root = self.roots[0]
        self.add_run('z_last', root=root)
        first = self.add_run('b_first', root=root)
        self.add_run('a_active', root=root, closed=False)
        accepted = self.add_run('00_accepted', root=root)
        (first.parent / '01_file').write_text('not a run')
        (first.parent / '02_link').symlink_to(first, target_is_directory=True)
        (first.parent / '03_dangling').symlink_to(first.parent / 'missing')
        unsafe_summary = first.parent / '04_summary_link'
        unsafe_summary.mkdir()
        (unsafe_summary / 'summary.json').symlink_to(first / 'summary.json')
        self.add_run('a_next_root', root=self.roots[1])
        full = self.local_manifest([str(accepted.relative_to(self.remote))])
        expected = [root + '/b_first', root + '/z_last', self.roots[1] + '/a_next_root']
        self.assertEqual([row['run'] for row in full['runs']], expected)
        self.assertEqual(full['skipped'], [
            {'run': root + '/a_active', 'reason': 'sandbox_not_confirmed_closed'},
            *({'root': value, 'reason': 'root_not_present'} for value in self.roots[2:]),
        ])
        capped = self.local_manifest([str(accepted.relative_to(self.remote))], limit=2)
        self.assertEqual([row['run'] for row in capped['runs']], expected[:2])
        self.assertEqual(capped['skipped'], full['skipped'][:1])

    def test_unaccepted_entry_metadata_errors_still_refuse(self):
        run = self.add_run()
        for method_name in ('is_symlink', 'is_dir'):
            original = getattr(Path, method_name)
            def checked(path):
                if path == run:
                    raise PermissionError('unaccepted metadata unavailable')
                return original(path)
            with self.subTest(method=method_name), \
                 patch.object(Path, method_name, checked), \
                 self.assertRaisesRegex(PermissionError, 'unaccepted metadata unavailable'):
                self.local_manifest()

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
        self.assertNotIn('selected_runs', json.loads(Path(first['attempt'], 'REQUEST.json').read_text()))
        for line in before.splitlines():
            receipt = json.loads(line)
            self.assertEqual(Path(receipt['path'], 'evidence.bin').read_bytes(), b'original evidence\x00\xff')
            self.assertEqual(hashlib.sha256(Path(receipt['manifest']).read_bytes()).hexdigest(), receipt['manifest_sha256'])

    def test_selected_copy_preserves_multi_root_history_and_skips_prior_acceptance(self):
        for root in self.roots:
            self.add_run(root=root)
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport):
            synchronize_completed(self.config, self.authorize)
            index = self.config.destination / 'accepted.jsonl'
            original = index.read_bytes()
            self.add_run('selected')
            self.add_run('unselected')
            self.add_run('other_root', root=self.roots[0])
            selection = (self.roots[0] + '/one', self.roots[-1] + '/selected')
            config = replace(self.config, selected_runs=selection, limit=2)
            copied = synchronize_completed(config, self.authorize)
            after = index.read_bytes()
            empty = synchronize_completed(config, self.authorize)
        self.assertEqual(copied['verified_runs'], 1)
        self.assertEqual(empty['verified_runs'], 0)
        self.assertTrue(after.startswith(original))
        added = json.loads(after[len(original):])
        self.assertEqual(added['run'], self.roots[-1] + '/selected')
        self.assertEqual(index.read_bytes(), after)
        request = json.loads(Path(copied['attempt'], 'REQUEST.json').read_text())
        self.assertEqual(request['roots'], list(self.roots))
        self.assertEqual(request['selected_runs'], list(selection))
        self.assertEqual(len(request['accepted']), 4)

    def test_unselected_runs_are_filtered_before_any_per_run_metadata(self):
        selected = self.add_run('selected')
        unselected = self.add_run('unselected')
        original_link, original_dir = Path.is_symlink, Path.is_dir
        def guard(method):
            def checked(path):
                if path == unselected or unselected in path.parents:
                    raise OSError('unselected metadata must not be accessed')
                return method(path)
            return checked
        with patch.object(Path, 'is_symlink', guard(original_link)), \
             patch.object(Path, 'is_dir', guard(original_dir)):
            manifest = self.local_manifest(selected_runs=(str(selected.relative_to(self.remote)),))
        self.assertEqual([r['run'] for r in manifest['runs']], [self.roots[-1] + '/selected'])

    def test_selection_preserves_existing_root_lexical_order_and_limit(self):
        paths = [self.add_run('z', root=self.roots[0]), self.add_run('a', root=self.roots[0]),
                 self.add_run('next', root=self.roots[1])]
        selected = tuple(str(p.relative_to(self.remote)) for p in reversed(paths))
        manifest = self.local_manifest(selected_runs=selected, limit=2)
        self.assertEqual([r['run'] for r in manifest['runs']],
                         [self.roots[0] + '/a', self.roots[0] + '/z'])

    def test_invalid_or_unauthorized_selection_refuses_before_transport(self):
        run = self.roots[-1] + '/one'
        invalid = [run, [run], (), (run, run), (True,), ([],), ('runs/other/model/one',),
                   (run + '/child',), ('/' + run,), (self.roots[-1] + '//one',),
                   (self.roots[-1] + '/../one',)]
        with patch('causes.campaign_snapshot.subprocess.run') as transport:
            for selection in invalid:
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    synchronize_completed(replace(self.config, selected_runs=selection), self.authorize)
            config = replace(self.config, selected_runs=(run,))
            for selection in (None, [], [self.roots[-1] + '/other']):
                def authorize(config):
                    value = self.authorize(config)
                    value['selected_runs'] = selection
                    return value
                with self.subTest(authorized_selection=selection), \
                     self.assertRaisesRegex(ValueError, 'authorization_selection'):
                    synchronize_completed(config, authorize)
            transport.assert_not_called()

    def test_received_unselected_manifest_is_rejected_before_copy(self):
        self.add_run('selected')
        self.add_run('unselected')
        config = replace(self.config, selected_runs=(self.roots[-1] + '/selected',))
        def bad_manifest(command, **kwargs):
            if command[0] == 'ssh':
                request = json.loads(kwargs['input'])
                request.pop('selected_runs')
                kwargs['input'] = json.dumps(request)
            return self.transport(command, **kwargs)
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=bad_manifest), \
             self.assertRaisesRegex(ValueError, 'unselected_manifest_run'):
            synchronize_completed(config, self.authorize)
        self.assertEqual(self.transport_calls, ['ssh'])
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())
        self.assertEqual(len(list((self.config.destination / 'attempts').glob('*/MANIFEST.json'))), 1)

    def test_selected_copy_enforces_actual_manifest_bytes_plus_configured_floor(self):
        self.add_run('selected')
        self.add_run('unselected')
        selection = (self.roots[-1] + '/selected',)
        manifest = self.local_manifest(selected_runs=selection)
        required = sum(row['bytes'] for row in manifest['runs'])
        floor = 64 * 1024**3
        config = replace(self.config, selected_runs=selection, free_space_floor_bytes=floor)
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport), \
             patch('causes.campaign_snapshot.shutil.disk_usage', return_value=SimpleNamespace(free=required + floor - 1)), \
             self.assertRaisesRegex(SnapshotSpaceHold, 'free-space floor') as caught:
            synchronize_completed(config, self.authorize)
        self.assertEqual(caught.exception.required_bytes, required)
        self.assertEqual(caught.exception.available_bytes, required + floor - 1)
        self.assertEqual(caught.exception.floor_bytes, floor)
        self.assertTrue((caught.exception.attempt / 'MANIFEST.json').is_file())
        self.assertEqual(self.transport_calls, ['ssh'])
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport), \
             patch('causes.campaign_snapshot.shutil.disk_usage', return_value=SimpleNamespace(free=required + floor)):
            result = synchronize_completed(config, self.authorize)
        self.assertEqual(result['verified_runs'], 1)
        self.assertEqual(result['verified_bytes'], required)
        self.assertEqual(self.transport_calls, ['ssh', 'ssh', 'rsync'])

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
