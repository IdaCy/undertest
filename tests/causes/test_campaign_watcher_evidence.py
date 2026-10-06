import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from causes import campaign_handoff as h
from causes import campaign_readiness as readiness
from causes import campaign_watcher_evidence as evidence
from causes import campaign_watcher_handoff as handoff


CHILD = '''import datetime, fcntl, hashlib, json, os, time
from pathlib import Path
source = Path(__file__)
config = json.loads(source.with_suffix('.config.json').read_bytes())
directory = Path(config['directory'])
with (directory / 'watch.lock').open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    value = {'pid': os.getpid(), 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'script_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
             'configuration_sha256': hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
             'interval_seconds': config['interval_seconds']}
    (directory / ('START_' + str(os.getpid()) + '.json')).write_text(json.dumps(value))
    while not (directory / 'TEST_EXIT').exists():
        if (directory / 'TEST_UNLOCK').exists():
            fcntl.flock(lock, fcntl.LOCK_UN)
            (directory / 'TEST_UNLOCKED').touch()
        time.sleep(.01)
'''


class Fixture:
    """Only this fixture child runs; every handoff lineage record is synthetic."""

    def __init__(self, root):
        self.root = root
        self.directory = root / 'watch'
        self.directory.mkdir()
        self.launch = root / 'launch'
        self.launch.mkdir()
        self.source = root / 'watcher.py'
        self.source.write_text(CHILD)
        self.installed = root / 'watcher.config.json'
        self.installed.write_bytes(h.encoded({'directory': str(self.directory), 'interval_seconds': 120,
                                              'watcher_sha256': h.pin(self.source)['sha256']}))
        self.argv = [sys.executable, str(self.source)]
        self.child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self.startup_path = self.directory / ('START_' + str(self.child.pid) + '.json')
        self.wait(lambda: self.startup_path.is_file() and h.lock_owned_by(self.directory / 'watch.lock', self.child.pid))
        ticks = Path(f'/proc/{self.child.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        self.identity = {'pid': self.child.pid, 'process_starttime_ticks': int(ticks), 'argv': self.argv}
        old = {'pid': 999999999, 'process_starttime_ticks': 1, 'argv': self.argv}
        old_source = h.pin(self.source, b'synthetic predecessor source')
        config = {'schema': 'causes.campaign_watcher_handoff.v1', 'directory': str(self.directory),
                  'implementation': h.pin(handoff.__file__), 'wrapper': h.pin(h.__file__),
                  'python': sys.executable, 'predecessor': {'process': old, 'source': old_source},
                  'successor': {'source': h.pin(self.source), 'configuration': h.pin(self.installed)},
                  'pause_paths': [str(root / 'PAUSE')],
                  'reviews': {'retirement': 'accepted_retirement', 'publication': 'accepted_publication'}}
        self.configuration = h.save_new(root, 'CONFIGURATION.json', config)
        proposal = h.save_new(root, 'PROPOSAL.json', {'watcher_handoff': self.configuration})
        archived = root / 'STOP.created'
        archived.write_bytes(b'owned synthetic predecessor STOP\n')
        marker = h.pin(self.directory / 'STOP', archived.read_bytes())
        self.ownership = h.save_new(root, 'STOP_OWNERSHIP.json', {'marker': marker, 'archived_marker': h.pin(archived)})
        retired = h.save_new(root, 'RETIREMENT.json', {
            'retired': True, 'lock_free': True, 'process': old, 'source': old_source,
            'configuration': self.configuration, 'proposal': proposal,
            'stop_ownership': self.ownership, 'owned_stop': marker,
            'lock_identity': list(handoff.lock_identity(self.directory / 'watch.lock'))})
        self.retirement_review = h.save_new(root, 'RETIREMENT_REVIEW.json', {'accepted_retirement': True, 'retirement': retired})
        publication = h.save_new(root, 'PUBLICATION.json', {'proposal': proposal,
                                    'current': [h.pin(self.source), h.pin(self.installed)]})
        publication_review = h.save_new(root, 'PUBLICATION_REVIEW.json', {'accepted_publication': True, 'result': publication})
        self.intent = {'action': 'release_owned_stop_and_start_one_watcher', 'configuration': self.configuration,
            'retirement': retired, 'retirement_review': self.retirement_review,
            'publication_result': publication, 'publication_review': publication_review,
            'source': h.pin(self.source), 'watcher_configuration': h.pin(self.installed),
            'wrapper': config['wrapper'], 'argv': self.argv, 'owned_stop': self.ownership}
        intent = h.save_new(self.launch, 'INTENT.json', self.intent)
        self.process = {**self.identity, 'intent_path': intent['path'], 'intent_sha256': intent['sha256']}
        process = h.save_new(self.launch, 'PROCESS.json', self.process)
        stopped = h.save_new(self.launch, 'STOP_RETIRED.json', {'ownership': self.ownership, 'intent': intent})
        self.result = {'configuration': self.configuration, 'process': self.process, 'process_receipt': process,
            'startup': h.pin(self.startup_path), 'source': h.pin(self.source), 'watcher_configuration': h.pin(self.installed),
            'sole_watch_lock_owner': self.child.pid, 'owned_stop_retired': stopped,
            'retirement': retired, 'publication_result': publication,
            'ordinary_cycle_and_actor_release_pending': True}
        self.result_pin = h.save_new(self.launch, 'RESULT.json', self.result)

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise AssertionError('owned fixture child exited')
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('owned fixture child did not become ready')

    def rewrite(self, reference, value):
        path = Path(reference['path'])
        path.write_bytes(h.encoded(value) + b'\n')
        return h.pin(path)

    def refresh_result(self):
        self.result_pin = self.rewrite(self.result_pin, self.result)

    def refresh_intent(self):
        intent = self.rewrite(h.pin(self.launch / 'INTENT.json'), self.intent)
        self.process['intent_sha256'] = intent['sha256']
        self.result['process_receipt'] = self.rewrite(self.result['process_receipt'], self.process)
        self.result['owned_stop_retired'] = self.rewrite(self.result['owned_stop_retired'],
                                                        {'ownership': self.ownership, 'intent': intent})
        self.refresh_result()

    def close(self):
        # This marker and fallback signal address only the child created above.
        (self.directory / 'TEST_EXIT').touch()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            self.child.wait(timeout=5)


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as name:
        value = Fixture.__new__(Fixture)
        try:
            value.__init__(Path(name))
            yield value
        finally:
            if hasattr(value, 'child'):
                value.close()


class WatcherEvidenceTests(unittest.TestCase):
    def refused(self, f, reason=None):
        output = f.root / 'refused'
        with self.assertRaises((ValueError, KeyError, FileNotFoundError)) as caught:
            evidence.capture(f.configuration, f.result_pin, output)
        if reason:
            self.assertIn(reason, str(caught.exception))
        failure = h.checked(h.pin(output / 'FAILED.json'))
        self.assertTrue(failure['no_runtime_controls_changed'])
        self.assertFalse((output / 'SUCCESSOR_EVIDENCE.json').exists())
        return failure

    def test_real_capture_projects_into_readiness_without_actions_or_acceptance(self):
        with fixture() as f:
            before = {str(p): h.pin(p) for p in (f.source, f.installed, f.startup_path,
                                                f.launch / 'PROCESS.json', f.launch / 'RESULT.json')}
            with mock.patch('subprocess.Popen', side_effect=AssertionError('capture spawned a process')), \
                 mock.patch('os.kill', side_effect=AssertionError('capture signaled a process')), \
                 mock.patch('os.execv', side_effect=AssertionError('capture executed a process')):
                pins = evidence.capture(f.configuration, f.result_pin, f.root / 'capture')
            current, successor = h.checked(pins['current']), h.checked(pins['successor'])
            self.assertEqual(current['process'], f.identity)
            self.assertEqual(current['lock_file_identity'], dict(zip(('st_dev', 'st_ino'), handoff.lock_identity(f.directory / 'watch.lock'))))
            self.assertEqual(len(current['raw_pins']), 4)
            self.assertTrue(current['read_only'])
            self.assertTrue(current['lock_owned'] and current['stop_absent'])
            self.assertFalse(any(k.startswith('accepted_') or 'authority' in k for k in current.keys() | successor.keys()))
            review = h.save_new(f.root, 'SYNTHETIC_REVIEW.json', {'accepted_JP_local_watcher_successor': True, 'evidence': pins['successor']})
            inputs = {'watcher_successor': pins['successor'], 'watcher_successor_review': review,
                      'watcher_current_observation': pins['current']}
            started = dt.datetime.fromisoformat(h.checked(h.pin(f.startup_path))['started_at'])
            checked = readiness.watcher_check(inputs, h.pin(f.source), started, started)
            self.assertEqual(checked, (successor, current))
            self.assertEqual(before, {p: h.pin(p) for p in before})
            self.assertIsNone(f.child.poll())

    def test_changed_installed_source_or_configuration_refuses(self):
        for target in ('source', 'installed'):
            with self.subTest(target=target), fixture() as f:
                path = getattr(f, target)
                path.write_bytes(path.read_bytes() + b'\n')
                self.refused(f, 'handoff_input_pin_changed')
                self.assertIsNone(f.child.poll())

    def test_stop_presence_including_dangling_symlink_refuses_without_removal(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), fixture() as f:
                stop = f.directory / 'STOP'
                if symlink:
                    stop.symlink_to(f.root / 'absent')
                else:
                    stop.write_bytes(b'foreign STOP')
                self.refused(f, 'watcher_stop_present')
                self.assertTrue(os.path.lexists(stop))
                self.assertIsNone(f.child.poll())

    def test_self_consistent_wrong_process_start_ticks_refuses_with_raw_evidence(self):
        with fixture() as f:
            f.process['process_starttime_ticks'] += 1
            f.result['process_receipt'] = f.rewrite(f.result['process_receipt'], f.process)
            f.refresh_result()
            self.refused(f, 'watcher_live_process_changed')
            self.assertTrue((f.root / 'refused/proc_stat_before').is_file())
            self.assertTrue((f.root / 'refused/proc_stat_after').is_file())

    def test_historical_lock_inode_and_actual_sole_owner_are_both_required(self):
        for changed_inode in (True, False):
            with self.subTest(changed_inode=changed_inode), fixture() as f:
                lock = f.directory / 'watch.lock'
                if changed_inode:
                    lock.rename(f.directory / 'retained_original_lock')
                    lock.touch()
                    reason = 'watcher_lock_inode_changed'
                else:
                    (f.directory / 'TEST_UNLOCK').touch()
                    f.wait(lambda: (f.directory / 'TEST_UNLOCKED').is_file())
                    reason = 'watcher_not_sole_lock_owner'
                self.refused(f, reason)
                self.assertIsNone(f.child.poll())

    def test_lineage_review_stop_shape_startup_hash_and_intent_path_tampering_refuse(self):
        for change in ('review', 'stop_shape', 'startup', 'intent_path', 'archive'):
            with self.subTest(change=change), fixture() as f:
                if change == 'review':
                    value = h.checked(f.retirement_review);value['accepted_retirement'] = False
                    f.intent['retirement_review'] = f.rewrite(f.retirement_review, value)
                    f.refresh_intent()
                elif change == 'stop_shape':
                    value = h.checked(f.result['owned_stop_retired'])
                    value['owned_stop'] = value.pop('ownership')
                    f.result['owned_stop_retired'] = f.rewrite(f.result['owned_stop_retired'], value)
                    f.refresh_result()
                elif change == 'startup':
                    value = h.checked(f.result['startup']);value['configuration_sha256'] = '0' * 64
                    f.result['startup'] = f.rewrite(f.result['startup'], value)
                    f.refresh_result()
                elif change == 'intent_path':
                    other = f.root / 'INTENT.json';other.write_bytes((f.launch / 'INTENT.json').read_bytes())
                    f.process['intent_path'] = str(other)
                    f.result['process_receipt'] = f.rewrite(f.result['process_receipt'], f.process)
                    f.refresh_result()
                else:
                    (f.root / 'STOP.created').write_bytes(b'changed retained owned STOP')
                self.refused(f)
                self.assertIsNone(f.child.poll())

    def test_existing_output_is_preserved_before_any_capture(self):
        with fixture() as f:
            output = f.root / 'existing';output.mkdir()
            retained = output / 'sentinel';retained.write_bytes(b'preserve exact bytes')
            before = h.pin(retained)
            with self.assertRaises(FileExistsError):
                evidence.capture(f.configuration, f.result_pin, output)
            self.assertEqual(h.pin(retained), before)
            self.assertEqual(list(output.iterdir()), [retained])
            self.assertIsNone(f.child.poll())

    def test_output_overlap_cannot_create_stop_pause_or_launch_metadata(self):
        with fixture() as f:
            targets = (f.directory / 'STOP', f.root / 'PAUSE', f.directory / 'new_capture', f.launch / 'new_capture')
            for target in targets:
                with self.subTest(target=target.name):
                    self.assertFalse(os.path.lexists(target))
                    with self.assertRaises(ValueError):
                        evidence.capture(f.configuration, f.result_pin, target)
                    self.assertFalse(os.path.lexists(target))
                    self.assertIsNone(f.child.poll())


if __name__ == '__main__':
    unittest.main()
