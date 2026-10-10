from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes import campaign_snapshot as snapshot
from causes import campaign_snapshot_watch as watch


class SnapshotWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.remote = self.base / 'remote'
        self.root = 'runs/cohort/model'
        self.config = snapshot.SnapshotConfig(
            'fixture', str(self.remote), (self.root,), self.base / 'copies',
            free_space_floor_bytes=100, limit=2,
            selected_runs=tuple(self.root + '/' + name for name in ('a', 'b', 'c', 'd')))
        self.directory = self.base / 'watch'
        self.calls = []

    def add(self, name, closed=True):
        run = self.remote / self.root / name
        run.mkdir(parents=True, exist_ok=True)
        (run / 'summary.json').write_text(json.dumps({
            'sandbox_closure': {'state': 'closed' if closed else 'open',
                                'overlay_mounted': not closed},
            'finished_at': '2000-01-01T00:00:00+00:00'}))
        (run / 'evidence.bin').write_bytes(b'preserved evidence')
        return run

    def authorize(self, config):
        return dict(host=config.host, remote_base=config.remote_base,
                    roots=list(config.roots), selected_runs=list(config.selected_runs),
                    evidence={'registration': 'fixed'})

    def transport(self, argv, **kwargs):
        self.calls.append(argv[0])
        if argv[0] == 'ssh':
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO(kwargs['input'])), redirect_stdout(output):
                exec(compile(snapshot.REMOTE_CODE, '<native manifest>', 'exec'), {})
            return subprocess.CompletedProcess(argv, 0, output.getvalue(), '')
        self.assertEqual(argv[0], 'rsync')
        listing = next(v.split('=', 1)[1] for v in argv if v.startswith('--files-from='))
        for raw in Path(listing).read_bytes().split(b'\0'):
            if raw:
                relative = raw.decode()
                target = Path(argv[-1]) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.remote / relative, target)
        return subprocess.CompletedProcess(argv, 0)

    def run_watch(self, wait=None, transport=None):
        with patch.object(snapshot.subprocess, 'run', side_effect=transport or self.transport), \
             patch.object(watch, '_wait', side_effect=wait or AssertionError('unexpected wait')):
            return watch.watch_completed(self.config, self.authorize, self.directory)

    def test_full_batches_catch_up_without_sleep_and_preserve_prior_receipts(self):
        for name in ('a', 'b', 'c', 'd', 'unselected'):
            self.add(name)
        with patch.object(snapshot.subprocess, 'run', side_effect=self.transport):
            snapshot.synchronize_completed(replace(self.config, limit=1), self.authorize)
        index = self.config.destination / 'accepted.jsonl'
        before = index.read_bytes()
        self.calls.clear()
        result = self.run_watch()
        after = index.read_bytes()
        self.assertEqual(result['reason'], 'selection_complete')
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(after.splitlines()), 4)
        self.assertEqual(self.calls, ['ssh', 'rsync', 'ssh', 'rsync'])
        for line in after.splitlines():
            row = json.loads(line)
            self.assertEqual(Path(row['path'], 'evidence.bin').read_bytes(), b'preserved evidence')
        self.assertEqual(json.loads((self.directory / '00000000/RESULT.json').read_text())[
            'native_result']['verified_runs'], 2)

    def test_empty_pass_waits_then_copies_newly_closed_selected_run(self):
        self.config = replace(self.config, selected_runs=(self.root + '/a',))
        self.add('a', closed=False)
        waits = []
        def wait(stop, seconds):
            waits.append(seconds)
            self.add('a')
        self.run_watch(wait=wait)
        self.assertEqual(waits, [60])
        self.assertEqual(self.calls, ['ssh', 'ssh', 'rsync'])

    def test_space_hold_rechecks_only_local_space_then_uses_fresh_native_manifest(self):
        self.config = replace(self.config, selected_runs=(self.root + '/a',))
        self.add('a')
        available, waits = [0], []
        def wait(stop, seconds):
            waits.append(seconds)
            if len(waits) == 2:
                available[0] = 10_000
        with patch.object(snapshot.shutil, 'disk_usage', side_effect=lambda _: SimpleNamespace(free=available[0])):
            self.run_watch(wait=wait)
        self.assertEqual(self.calls, ['ssh', 'ssh', 'rsync'])
        held = json.loads((self.directory / '00000001/RESULT.json').read_text())
        self.assertEqual(held['kind'], 'storage_held')
        self.assertFalse(held['remote_call'])
        self.assertEqual(held['hold']['floor_bytes'], 100)
        self.assertTrue(Path(held['hold']['attempt'], 'MANIFEST.json').is_file())
        self.assertEqual(len((self.config.destination / 'accepted.jsonl').read_bytes().splitlines()), 1)

    def test_unexpected_transfer_failure_latches_without_retry_or_index_append(self):
        self.add('a')
        def fail(argv, **kwargs):
            if argv[0] == 'rsync':
                self.calls.append('rsync')
                return subprocess.CompletedProcess(argv, 1)
            return self.transport(argv, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'rsync failed'):
            self.run_watch(transport=fail)
        self.assertTrue((self.directory / 'FAILED.json').is_file())
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())
        self.assertEqual(self.calls, ['ssh', 'rsync'])
        with self.assertRaises(FileExistsError):
            self.run_watch()
        self.assertEqual(self.calls, ['ssh', 'rsync'])

    def test_authorization_change_after_copy_refuses_acceptance_and_latches(self):
        self.add('a')
        changed = [False]
        original_authorize = self.authorize
        def authorize(config):
            value = original_authorize(config)
            value['evidence']['registration'] = 'changed' if changed[0] else 'fixed'
            return value
        self.authorize = authorize
        def transport(argv, **kwargs):
            value = self.transport(argv, **kwargs)
            if argv[0] == 'rsync':
                changed[0] = True
            return value
        with self.assertRaisesRegex(ValueError, 'authorization_changed'):
            self.run_watch(transport=transport)
        self.assertTrue((self.directory / 'FAILED.json').is_file())
        self.assertFalse((self.config.destination / 'accepted.jsonl').exists())

    def test_stop_does_not_interrupt_inflight_native_copy(self):
        self.config = replace(self.config, limit=1)
        self.add('a')
        self.add('b')
        def transport(argv, **kwargs):
            if argv[0] == 'rsync':
                (self.directory / 'STOP').write_text('owned stop')
            return self.transport(argv, **kwargs)
        result = self.run_watch(transport=transport)
        self.assertEqual(result['reason'], 'owned_stop')
        self.assertEqual(self.calls, ['ssh', 'rsync'])
        self.assertEqual(len((self.config.destination / 'accepted.jsonl').read_bytes().splitlines()), 1)
        self.assertTrue((self.directory / '00000000/RESULT.json').is_file())

    def test_numeric_authorization_change_between_passes_is_not_equal_evidence(self):
        self.config = replace(self.config, selected_runs=(self.root + '/a',))
        self.add('a', closed=False)
        original_authorize = self.authorize
        version = [1]
        def authorize(config):
            value = original_authorize(config)
            value['evidence']['version'] = version[0]
            return value
        self.authorize = authorize
        def wait(stop, seconds):
            if version[0] is True:
                stop.write_text('test prevents an unbounded incorrect loop')
            version[0] = True
        with self.assertRaisesRegex(ValueError, 'authorization_changed'):
            self.run_watch(wait=wait)
        self.assertEqual(self.calls, ['ssh'])
        self.assertTrue((self.directory / 'FAILED.json').is_file())

    def test_index_receipt_rewrite_between_passes_latches_before_next_remote_read(self):
        self.config = replace(self.config, selected_runs=(self.root + '/a', self.root + '/b'))
        self.add('a')
        self.add('b', closed=False)
        def wait(stop, seconds):
            index = self.config.destination / 'accepted.jsonl'
            receipt = json.loads(index.read_bytes())
            receipt['verified_at'] = 'rewritten'
            index.write_text(json.dumps(receipt) + '\n')
        with self.assertRaisesRegex(ValueError, 'index_prefix_changed'):
            self.run_watch(wait=wait)
        self.assertEqual(self.calls, ['ssh', 'rsync'])
        self.assertTrue((self.directory / 'FAILED.json').is_file())

    def test_completed_selection_never_reprobes_remote(self):
        self.config = replace(self.config, selected_runs=(self.root + '/a',))
        self.add('a')
        with patch.object(snapshot.subprocess, 'run', side_effect=self.transport):
            snapshot.synchronize_completed(self.config, self.authorize)
        self.calls.clear()
        self.run_watch()
        self.assertEqual(self.calls, [])

    def test_missing_selection_refuses_before_directory_or_transport(self):
        self.config = replace(self.config, selected_runs=None)
        with self.assertRaisesRegex(ValueError, 'finite_selection'):
            self.run_watch()
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
