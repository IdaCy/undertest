import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from causes import campaign_reader_watch as watch


class RouteStateCarryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.output = self.base / 'reader'
        self.accepted = self.base / 'accepted.jsonl'; self.accepted.write_bytes(b'')
        self.pause = self.base / 'PAUSE'; self.pause.touch()
        self.old = self.write('OLD.json', {'pins': {'configuration': 'unchanged'}})
        self.journal = self.base / 'closed.jsonl'
        self.rows(self.journal, [dict(event='claimed', key='old/early'), dict(event='finished', key='old/early'),
            dict(event='reader_route_failure', consecutive_failed_contexts=1, key='old/early'),
            dict(event='watcher_paused', new_admissions_stopped=True)])
        self.proof = dict(schema='causes.reader_route_state_carry.v1', accepted_reader_route_state_carry=True,
            output=str(self.output), new_identity=self.identity(), predecessor_identity=self.old,
            journal_baseline=self.pin(self.journal), consecutive_failed_contexts=1, quarantine=None, evidence=[self.old])
        self.ref = self.write('CARRY.json', self.proof)

    def rows(self, path, rows):
        path.write_bytes(b''.join(watch.reader.encoded(row) + b'\n' for row in rows))

    def pin(self, path):
        raw = path.read_bytes()
        return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    def write(self, name, value):
        path = self.base / name; path.write_text(json.dumps(value)); return self.pin(path)

    def identity(self):
        return dict(pins={'qualified': 'same'}, selected_sha256=watch.reader.digest({}),
                    accepted_index=str(self.accepted), workers=2)

    def watcher(self, carry=True):
        return watch.Watcher({}, {}, self.base, {'qualified': 'same'}, self.accepted, self.output,
                             self.pause, workers=2, route_state_carry=self.ref if carry else None)

    def test_durable_once_before_admission_without_new_attempt(self):
        w = self.watcher(); w.run(); rows = watch.jsonlines(w.journal, strict=True)
        self.assertEqual(rows[0]['event'], 'reader_route_state_carried'); self.assertEqual(rows[0]['carry'], self.ref)
        self.assertEqual(w.failures, 1); self.assertIsNone(w.quarantine)
        self.assertFalse(any(row['event'] in ('claimed', 'reader_route_failure') for row in rows))
        w = self.watcher(); w.run(); self.assertEqual(w.failures, 1)
        self.assertEqual(sum(row['event'] == 'reader_route_state_carried' for row in watch.jsonlines(w.journal)), 1)

    def test_success_resets_and_restart_does_not_reinherit(self):
        w = self.watcher(); w.run()
        w.observe_result('new/early', {'instruments': {'presence': {'status': 'validated'}}})
        w = self.watcher(); w.run(); self.assertEqual(w.failures, 0)

    def test_threshold_and_quarantine_survive_restart(self):
        failed = {'instruments': {'presence': {'status': 'technical_missing', 'technical_reason': 'ValueError'}}}
        w = self.watcher(); w.run(); w.observe_result('new/early', failed)
        self.assertEqual(w.failures, 2); self.assertIsNone(w.quarantine)
        w = self.watcher(); w.run(); self.assertEqual(w.failures, 2); w.observe_result('new/end', failed)
        self.assertEqual(w.failures, 3); self.assertTrue(w.route_blocked()); expected = w.quarantine
        w = self.watcher(); w.run(); self.assertEqual(w.failures, 3); self.assertEqual(w.quarantine, expected)

    def test_invalid_carry_refuses_before_journal(self):
        for changes in ({'consecutive_failed_contexts': True}, {'consecutive_failed_contexts': -1},
            {'consecutive_failed_contexts': 3}, {'consecutive_failed_contexts': 0},
            {'quarantine': {'reason': 'blocked'}}, {'accepted_reader_route_state_carry': False},
            {'new_identity': {}}, {'evidence': []}):
            with self.subTest(changes=changes):
                self.ref = self.write('CARRY.json', dict(self.proof, **changes))
                with self.assertRaises(ValueError): self.watcher().run()
                self.assertFalse((self.output / '.watch/journal.jsonl').exists())

    def test_missing_or_changed_proof_cannot_resume(self):
        self.watcher().run()
        with self.assertRaisesRegex(ValueError, 'carry_required|identity_changed'): self.watcher(False).run()
        self.ref = self.write('CHANGED.json', dict(self.proof, reviewer_note='distinct proof'))
        with self.assertRaisesRegex(ValueError, 'carry_journal_changed|carry_receipt_changed|identity_changed'): self.watcher().run()

    def test_missing_duplicate_late_or_torn_carry_event_refuses(self):
        w = self.watcher(); w.run(); raw = w.journal.read_bytes(); rows = watch.jsonlines(w.journal)
        for rows2 in (rows[1:], rows + [rows[0]], [rows[1], rows[0], *rows[2:]]):
            with self.subTest(rows=rows2):
                self.rows(w.journal, rows2)
                with self.assertRaises(ValueError): self.watcher().run()
        w.journal.write_bytes(raw[:-1])
        with self.assertRaisesRegex(ValueError, 'torn_reader_journal'): self.watcher().run()

    def test_empty_or_deleted_journal_cannot_reset_later_failures(self):
        w = self.watcher(); w.run()
        w.observe_result('new/early', {'instruments': {'presence': {
            'status': 'technical_missing', 'technical_reason': 'ValueError'}}})
        self.assertEqual(w.failures, 2)
        w.journal.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'journal_missing'): self.watcher().run()
        w.journal.unlink()
        with self.assertRaisesRegex(ValueError, 'journal_missing'): self.watcher().run()

    def test_crash_between_identity_and_event_restores_once(self):
        w = self.watcher(); watch.save(w.meta / 'IDENTITY.json', dict(self.identity(), route_state_carry=self.ref))
        with self.assertRaisesRegex(ValueError, 'identity_changed'): self.watcher(False).run()
        w.run(); self.assertEqual(w.failures, 1)
        self.assertEqual(watch.jsonlines(w.journal)[0]['event'], 'reader_route_state_carried')

    def test_changed_source_and_unfinished_claim_refuse(self):
        with self.journal.open('ab') as stream:
            stream.write(watch.reader.encoded(dict(event='claimed', key='unfinished')) + b'\n')
        with self.assertRaises(ValueError): self.watcher().run()
        self.ref = self.write('CARRY2.json', dict(self.proof, journal_baseline=self.pin(self.journal)))
        with self.assertRaisesRegex(ValueError, 'unfinished_or_duplicate'): self.watcher().run()

    def test_input_succession_is_distinct(self):
        with self.assertRaisesRegex(ValueError, 'are_distinct'):
            watch.Watcher({}, {}, self.base, {}, self.accepted, self.output, self.pause,
                          input_succession=self.ref, route_state_carry=self.ref)

    def test_default_identity_and_state_unchanged(self):
        w = self.watcher(False); w.run()
        self.assertEqual(json.loads((w.meta / 'IDENTITY.json').read_bytes()), self.identity())
        self.assertEqual(w.failures, 0)
        self.assertFalse(any(row['event'] == 'reader_route_state_carried' for row in watch.jsonlines(w.journal)))


if __name__ == '__main__': unittest.main()
