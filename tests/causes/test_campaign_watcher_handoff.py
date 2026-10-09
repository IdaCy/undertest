import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from causes import campaign_handoff as h
from causes import campaign_watch as watch
from causes import campaign_watcher_handoff as handoff


class WatcherFixture:
    def __init__(self, root):
        self.root = root
        self.directory = root / 'watch'
        self.directory.mkdir()
        self.source = root / 'watcher.py'
        self.source.write_bytes(Path(watch.__file__).read_bytes())
        self.config_path = root / 'watcher.config.json'
        self.check = root / 'check.py'
        self.check.write_text("print('fixture observation')\n")
        self.periodic = root / 'periodic.py'
        self.periodic.write_text("print('fixture periodic observation')\n")
        self.config = {'schema': 'causes.campaign_watch_config.v1', 'directory': str(self.directory),
                       'script_directory': str(root), 'watcher_sha256': h.pin(self.source)['sha256'],
                       'interval_seconds': 120, 'checks': [self.row(self.check)],
                       'periodic': {'every': 5, 'check': self.row(self.periodic)}}
        self.config_path.write_text(json.dumps(self.config))
        self.before = h.pin(self.source)
        self.before_config = h.pin(self.config_path)
        self.child = subprocess.Popen([sys.executable, str(self.source)], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise RuntimeError('fixture_watcher_failed')
            if (self.directory / ('START_' + str(self.child.pid) + '.json')).exists() and list(self.directory.glob('*/RESULT.json')):
                break
            time.sleep(.02)
        else:
            raise RuntimeError('fixture_watcher_not_ready')
        fields = (Path('/proc') / str(self.child.pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        self.process = {'pid': self.child.pid, 'process_starttime_ticks': int(fields[19]),
                        'argv': [sys.executable, str(self.source)]}
        self.candidate = root / 'candidate.py'
        self.candidate.write_bytes(self.source.read_bytes() + b'\n')
        self.next_config = {**self.config, 'watcher_sha256': h.pin(self.candidate)['sha256']}
        self.candidate_config = root / 'candidate.json'
        self.candidate_config.write_text(json.dumps(self.next_config))
        self.after = {**h.pin(self.candidate), 'path': str(self.source)}
        self.after_config = {**h.pin(self.candidate_config), 'path': str(self.config_path)}
        value = {'schema': 'causes.campaign_watcher_handoff.v1',
                 'implementation': h.pin(Path(handoff.__file__)), 'wrapper': h.pin(Path(h.__file__)),
                 'python': sys.executable, 'directory': str(self.directory),
                 'pause_paths': [str(root / 'PAUSE')], 'predecessor': {'process': self.process, 'source': self.before},
                 'successor': {'source': self.after, 'configuration': self.after_config},
                 'reviews': {'candidate': 'accepted_candidate', 'retirement': 'accepted_retirement',
                             'publication': 'accepted_publication'}}
        self.configuration = h.save_new(root, 'HANDOFF.json', value)
        self.proposal_value = {'watcher_handoff': self.configuration, 'requires_watcher_retirement': True,
            'rows': [{'target': str(self.source), 'before': self.before, 'after': h.pin(self.candidate)},
                     {'target': str(self.config_path), 'before': self.before_config, 'after': h.pin(self.candidate_config)}],
            'preserved': [{'target': str(self.check), 'pin': h.pin(self.check)}]}
        self.proposal = h.save_new(root, 'PROPOSAL.json', self.proposal_value)
        self.review = h.save_new(root, 'PROPOSAL_REVIEW.json', {'accepted_candidate': True, 'proposal': self.proposal})
        self.helper = handoff.WatcherHandoff(self.configuration)

    @staticmethod
    def row(path):
        return {'script': path.name, 'timeout_seconds': 3, 'source': h.pin(path)}

    def close(self):
        stop = self.directory / 'STOP'
        if not stop.exists():
            stop.write_text('fixture cleanup')
        if self.child.poll() is None:
            self.child.wait(timeout=8)

    def retire_and_publish(self):
        retired = self.helper.drain(self.proposal, self.review, self.root / 'retirement')
        self.child.wait(timeout=3)
        retirement_review = h.save_new(self.root, 'RETIREMENT_REVIEW.json', {
            'accepted_retirement': True, 'retirement': retired})
        self.source.write_bytes(self.candidate.read_bytes())
        self.config_path.write_bytes(self.candidate_config.read_bytes())
        publication = h.save_new(self.root, 'PUBLICATION.json', {'proposal': self.proposal,
            'applied': [row['target'] for row in self.proposal_value['rows']],
            'current': [h.pin(self.source), h.pin(self.config_path), h.pin(self.check)]})
        review = h.save_new(self.root, 'PUBLICATION_REVIEW.json', {
            'accepted_publication': True, 'result': publication})
        return retired, retirement_review, publication, review


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as temp:
        value = WatcherFixture(Path(temp))
        try:
            yield value
        finally:
            value.close()


class WatcherHandoffTests(unittest.TestCase):
    def test_real_watcher_drains_and_one_same_pid_exec_successor_owns_lock(self):
        with fixture() as f:
            arguments = f.retire_and_publish()
            retired = h.checked(arguments[0])
            self.assertTrue(retired['no_signals_sent'])
            self.assertEqual(h.checked(retired['final_completed_cycle'])['checks'][0]['returncode'], 0)
            result = f.helper.start(*arguments, f.root / 'successor')
            actual = h.checked(result)
            self.assertTrue(actual['ordinary_cycle_and_actor_release_pending'])
            self.assertFalse((f.directory / 'STOP').exists())
            self.assertEqual(actual['process']['pid'], actual['sole_watch_lock_owner'])
            self.assertEqual(actual['process']['argv'], f.process['argv'])
            self.assertTrue(h.lock_owned_by(f.directory / 'watch.lock', actual['process']['pid']))
            # Only the fixture successor is asked to finish; no signal is sent.
            (f.directory / 'STOP').write_text('fixture cleanup')
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not h.lock_free(f.directory / 'watch.lock'):
                time.sleep(.02)
            self.assertTrue(h.lock_free(f.directory / 'watch.lock'))
            os.waitpid(actual['process']['pid'], 0)

    def test_incomplete_newest_cycle_cannot_reuse_older_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            older = root / '20260101T010000Z_aaaaaaaa'
            newer = root / '20260101T010100Z_bbbbbbbb'
            older.mkdir(); newer.mkdir()
            h.save_new(older, 'RESULT.json', {'checks': [], 'ended_at': 'closed'})
            with self.assertRaises(FileNotFoundError):
                handoff.newest_completed_cycle(root)

    def test_foreign_stop_and_pause_refuse_without_process_or_marker_change(self):
        with fixture() as f:
            stop = f.directory / 'STOP'
            stop.write_text('unowned marker')
            before = h.pin(stop)
            with self.assertRaisesRegex(ValueError, 'watcher_stop_already_exists'):
                f.helper.drain(f.proposal, f.review, f.root / 'refused')
            self.assertEqual(h.pin(stop), before)
            failed = json.loads((f.root / 'refused/FAILED.json').read_bytes())
            self.assertFalse(failed['stop_created'])
            pause = f.root / 'PAUSE'
            pause.write_text('user pause')
            with self.assertRaisesRegex(ValueError, 'pause_blocks'):
                handoff.WatcherHandoff(f.configuration)
            self.assertEqual(pause.read_text(), 'user pause')

    def test_changed_stop_identity_retains_marker_and_no_successor(self):
        with fixture() as f:
            arguments = f.retire_and_publish()
            stop = f.directory / 'STOP'
            raw = stop.read_bytes()
            stop.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'owned_barrier_identity_changed'):
                f.helper.start(*arguments, f.root / 'refused_start')
            self.assertEqual(stop.read_bytes(), raw)
            failure = json.loads((f.root / 'refused_start/FAILED.json').read_bytes())
            self.assertFalse(failure['owned_stop_removed'])
            self.assertFalse(failure['successor_spawned'])
            self.assertFalse((f.root / 'refused_start/SPAWN.json').exists())

    def test_cli_requires_explicit_execution_before_loading_configuration(self):
        result = subprocess.run([sys.executable, '-m', 'causes.campaign_watcher_handoff',
            '--configuration', '/unused/config.json', '--output', '/unused/output', 'drain',
            '--proposal', '/unused/proposal.json', '--review', '/unused/review.json'],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('explicit_watcher_handoff_execution_required', result.stderr)


if __name__ == '__main__':
    unittest.main()
