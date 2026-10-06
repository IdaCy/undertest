import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from causes.campaign_dispatcher import Dispatcher
from tests.causes.test_campaign_dispatcher import finish, job


class AdmissionControlsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pause = self.root / 'PAUSE'
        self.global_pause = self.root / 'GLOBAL_PAUSE'
        self.barrier = self.root / 'ADMISSION_BARRIER'
        self.calls, self.results, self.errors = [], [], []

    def make(self, runner, **kwargs):
        return Dispatcher(
            [job('first'), job('second')],
            {'model': 'fixture', 'endpoint': 'http://localhost:1',
             'dispatcher': {'disk_floor_bytes': 0}},
            self.root / 'runs', workers=1, max_full=0,
            pause_file=self.pause, global_pause_file=self.global_pause,
            runner=runner, poll_seconds=.002, **kwargs)

    def events(self, dispatcher):
        path = dispatcher.meta / 'journal.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.002)
        self.fail('fixture condition not reached')

    def start(self, dispatcher):
        def run():
            try:
                self.results.append(dispatcher.run())
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        def cleanup():
            self.global_pause.touch()
            thread.join(4)
        self.addCleanup(cleanup)
        return thread

    def test_optional_controls_preserve_default_admission(self):
        def runner(*args):
            self.calls.append(args[3]['run_id'])
            result = finish(*args)
            self.pause.touch()
            return result
        dispatcher = self.make(runner)
        self.assertFalse(dispatcher.admission_barred())
        self.assertEqual(dispatcher.run()['pending'], 1)
        self.assertEqual(self.calls, ['first'])

    def test_either_pause_stops_new_claims(self):
        for marker in (self.pause, self.global_pause):
            with self.subTest(marker=marker.name):
                marker.touch()
                dispatcher = self.make(lambda *args: self.fail('called behind pause'))
                self.assertEqual(dispatcher.run()['status'], 'paused')
                self.assertFalse(any(e['event'] == 'claimed' for e in self.events(dispatcher)))
                marker.unlink()

    def test_explicit_missing_barrier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'startup_barrier_required'):
            self.make(lambda *args: None, start_barrier_file=self.barrier)

    def test_live_waiting_owner_and_final_barrier_recheck(self):
        self.barrier.touch()
        def runner(*args):
            self.calls.append(args[3]['run_id'])
            result = finish(*args)
            self.pause.touch()
            return result
        dispatcher = self.make(runner, start_barrier_file=self.barrier)
        original = dispatcher.reconcile
        recreated = threading.Event()
        def reconcile(value):
            result = original(value)
            waiting = any(e['event'] == 'admission_barrier_waiting' for e in self.events(dispatcher))
            if waiting and not self.barrier.exists() and not recreated.is_set():
                self.barrier.touch()
                recreated.set()
            return result
        dispatcher.reconcile = reconcile
        thread = self.start(dispatcher)
        self.wait_for(lambda: any(e['event'] == 'admission_barrier_waiting' for e in self.events(dispatcher)))
        self.assertTrue((dispatcher.meta / 'queue_identity.json').is_file())
        self.assertTrue(thread.is_alive())
        self.assertFalse(any(e['event'] == 'claimed' for e in self.events(dispatcher)))
        self.barrier.unlink()
        self.assertTrue(recreated.wait(3))
        self.assertEqual(self.calls, [])
        self.assertFalse(any(e['event'] == 'claimed' for e in self.events(dispatcher)))
        self.barrier.unlink()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertEqual(self.calls, ['first'])

    def test_global_pause_drains_an_active_call(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def runner(*args):
            started.set()
            self.assertTrue(release.wait(3))
            self.calls.append(args[3]['run_id'])
            return finish(*args)
        dispatcher = self.make(runner)
        thread = self.start(dispatcher)
        self.assertTrue(started.wait(3))
        self.global_pause.touch()
        self.wait_for(lambda: any(e['event'] == 'pause_seen' for e in self.events(dispatcher)))
        self.assertTrue(thread.is_alive())
        release.set()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertEqual(self.calls, ['first'])
        self.assertEqual(self.results[0]['pending'], 1)


if __name__ == '__main__':
    unittest.main()
