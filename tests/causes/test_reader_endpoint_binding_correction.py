import copy
import json
from pathlib import Path
from unittest import TestCase

from causes import campaign_reader_watch as watch
from tests.causes import test_campaign_reader_watch as watcher_tests
from tests.causes import test_campaign_reader_inputs as input_tests


class EndpointBindingCorrectionTests(TestCase):
    refresh_manifest = watcher_tests.ReaderWatcherTests.refresh_manifest
    run_watcher = watcher_tests.ReaderWatcherTests.run_watcher
    endpoint_fixture = watcher_tests.ReaderWatcherTests.endpoint_fixture
    typed = input_tests.ReaderInputWatcherTests.typed
    pin = input_tests.ReaderInputWatcherTests.pin
    record = input_tests.ReaderInputWatcherTests.record

    def setUp(self):
        watcher_tests.ReaderWatcherTests.setUp(self)
        self.fixture = self.endpoint_fixture()
        self.accepted = self.accepted.with_name('reader_inputs.accepted.jsonl')
        self.accepted.write_text(json.dumps(self.typed()) + '\n')
        good = copy.deepcopy(self.fixture['pair'])
        # Equal JSON, genuinely different file bytes: the original defect.
        original = self.base / 'reserialized_original.json'
        original.write_text(json.dumps(self.fixture['original'], separators=(',', ':')))
        self.assertNotEqual(self.pin(original)['sha256'], self.frozen['model_file_sha256'])
        bad_value = dict(self.fixture['value'], original_config=self.pin(original))
        bad = self.fixture['accepted'](bad_value)
        self.frozen['endpoint_transitions'] = {self.fixture['root']: bad}
        self.previous = {'fixture': copy.deepcopy(self.frozen)}
        self.selected = copy.deepcopy(self.previous)
        self.selected['fixture']['endpoint_transitions'] = {self.fixture['root']: good}
        self.old_pins = {'selected_sha256': watch.reader.digest(self.previous),
                         'reader_config_sha256': 'same checker', 'prompts': {'rubric': 'same prompt'}}
        self.pins = dict(self.old_pins, selected_sha256=watch.reader.digest(self.selected))
        old = watch.Watcher(self.previous, {}, Path('.'), self.old_pins, self.accepted,
                            self.output, self.pause, workers=1, poll_seconds=0.005,
                            context_runner=lambda *a, **k: self.fail('bad binding called checker'))
        event = old.event
        def stop_after_terminal(kind, **fields):
            row = event(kind, **fields)
            if kind == 'finished':
                self.pause.touch()
            return row
        old.event = stop_after_terminal
        self.run_watcher(old)
        self.before_identity = (self.output / '.watch/IDENTITY.json').read_bytes()
        self.before_journal = old.journal.read_bytes()
        rows = watch.jsonlines(old.journal, strict=True)
        self.assertEqual(sum(r['event'] == 'claimed' for r in rows), 1)
        self.assertEqual(sum(r['event'] == 'finished' for r in rows), 1)
        self.quarantine = next(r for r in rows if r['event'] == 'reader_route_quarantined')
        self.assertIn('endpoint_snapshot_frozen_scope_mismatch', self.quarantine['reason'])
        baseline = self.base / 'closed_journal.jsonl'; baseline.write_bytes(self.before_journal)
        index = self.base / 'index.prefix.jsonl'; index.write_bytes(self.accepted.read_bytes())
        predecessor = self.pin(self.output / '.watch/IDENTITY.json')
        source = self.pin(Path(watch.__file__).resolve())
        retired = self.record('retirement.json', {
            'accepted_reader_retirement': True, 'process_absent': True,
            'watch_lock_acquired_nonblocking': True, 'all_claimed_terminal': True,
            'ambiguous_attempts': 0, 'predecessor_identity': predecessor, 'old_watcher': source,
            'output': str(self.output), 'journal_baseline': self.pin(baseline),
            'evidence': [self.record('retired_process.json', {'absent': True})]})
        self.identity = dict(json.loads(self.before_identity), pins=self.pins,
                             selected_sha256=watch.reader.digest(self.selected))
        self.review = {
            'accepted_reader_input_succession': True,
            'accepted_reader_endpoint_binding_correction': True,
            'kind': 'endpoint_binding_correction', 'output': str(self.output),
            'predecessor_identity': predecessor, 'new_identity': self.identity,
            'predecessor_selected': self.record('predecessor_selected.json', self.previous),
            'old_watcher': source, 'new_watcher': source, 'pin_replacements': [],
            'accepted_baseline': self.pin(index), 'journal_baseline': self.pin(baseline),
            'retirement_review': retired}
        self.reference = self.record('correction.json', self.review)

    def corrected(self, runner=None, reference=None):
        return watch.Watcher(self.selected, {}, Path('.'), self.pins, self.accepted,
            self.output, self.pause, workers=1, poll_seconds=0.005,
            context_runner=runner or (lambda *a, **k: self.fail('handled context replayed')),
            input_succession=reference or self.reference)

    def validate(self, review=None, selected=None, identity=None):
        ref = self.record('candidate.json', review or self.review)
        return watch.validate_input_succession(ref, identity or self.identity, self.output,
                                              selected=selected or self.selected)

    def test_actual_binding_correction_preserves_failure_then_runs_only_unhandled_stage(self):
        successor = self.corrected()
        self.run_watcher(successor)
        self.assertEqual(successor.quarantine, self.quarantine)
        self.assertEqual(successor.handled, {'mi27/fixture/early'})
        self.assertEqual(successor.completed, {'mi27/fixture/early'})
        # Explicit repair evidence is separate; succession itself never releases quarantine.
        path = Path(self.quarantine['required_resolution'])
        watch.save(path, {'status': 'qualified', 'quarantine_id': self.quarantine['quarantine_id'],
                          'qualification_evidence': self.reference})
        self.pause.unlink()
        calls = []
        def runner(client, config, prompts, messages, boundary, destination, **kwargs):
            calls.append(destination.name)
            destination.mkdir(parents=True)
            self.pause.touch()
            return {'instruments': {'presence': {'status': 'validated'}}}
        self.run_watcher(self.corrected(runner))
        self.run_watcher(self.corrected())
        self.assertEqual(calls, ['end'])
        self.assertEqual((self.output / '.watch/IDENTITY.json').read_bytes(), self.before_identity)
        self.assertTrue((self.output / '.watch/journal.jsonl').read_bytes().startswith(self.before_journal))
        rows = watch.jsonlines(self.output / '.watch/journal.jsonl', strict=True)
        self.assertEqual(sum(r['event'] == 'claimed' for r in rows), 2)
        self.assertEqual(sum(r['event'] == 'reader_route_requalified' for r in rows), 1)
        self.assertEqual(len(list((self.output / '.watch').glob('INPUT_SUCCESSION.*.json'))), 1)

    def test_same_typed_index_and_external_complete_prefix_required(self):
        for mode in ('different_index', 'live_baseline', 'torn_baseline', 'changed_prefix'):
            value, identity = copy.deepcopy(self.review), copy.deepcopy(self.identity)
            if mode == 'different_index':
                identity['accepted_index'] = str(self.accepted.with_name('other.jsonl'))
            elif mode == 'live_baseline':
                value['accepted_baseline'] = self.pin(self.accepted)
            else:
                p = self.base / (mode + '.jsonl')
                p.write_bytes(b'changed\n' if mode == 'changed_prefix' else b'torn')
                value['accepted_baseline'] = self.pin(p)
            value['new_identity'] = identity
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.validate(value, identity=identity)

    def test_frozen_job_config_keys_and_root_sets_cannot_change(self):
        for mode in ('job', 'config', 'run_keys', 'root_keys', 'numeric_type'):
            selected = copy.deepcopy(self.selected)
            if mode == 'job': selected['fixture']['job']['variant']['request'] = 'different'
            elif mode == 'config': selected['fixture']['config']['system'] = 'different'
            elif mode == 'run_keys': selected['other'] = selected.pop('fixture')
            elif mode == 'root_keys':
                pairs = selected['fixture']['endpoint_transitions']; pairs['runs/other'] = pairs.pop(self.fixture['root'])
            else: selected['fixture']['job']['allocation']['continue_to_end'] = 1
            identity = dict(self.identity, selected_sha256=watch.reader.digest(selected))
            review = dict(self.review, new_identity=identity)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.validate(review, selected, identity)

    def test_transition_may_only_replace_original_file_reference(self):
        changed = dict(self.fixture['value'], backend_label='different backend')
        selected = copy.deepcopy(self.selected)
        selected['fixture']['endpoint_transitions'][self.fixture['root']] = self.fixture['accepted'](changed)
        identity = dict(self.identity, selected_sha256=watch.reader.digest(selected))
        with self.assertRaises(ValueError):
            self.validate(dict(self.review, new_identity=identity), selected, identity)
        # An accepted pair with the same wrong original bytes still fails the real native predicate.
        with self.assertRaisesRegex(ValueError, 'endpoint_snapshot_frozen_scope_mismatch'):
            identity = dict(self.identity, selected_sha256=watch.reader.digest(self.previous))
            self.validate(dict(self.review, new_identity=identity), self.previous, identity)

    def test_retirement_closed_journal_and_derived_pin_digests_required(self):
        for mode in ('retirement', 'journal', 'old_selected', 'pin_digest', 'unaccepted'):
            review, identity = copy.deepcopy(self.review), copy.deepcopy(self.identity)
            if mode == 'retirement':
                retired = json.loads(watch.checked_pin(review['retirement_review']))
                retired['process_absent'] = False
                review['retirement_review'] = self.record('not_retired.json', retired)
            elif mode == 'journal':
                p = self.base / 'different_journal.jsonl'; p.write_bytes(b'{}\n')
                review['journal_baseline'] = self.pin(p)
            elif mode == 'old_selected': review['predecessor_selected'] = self.record('wrong_previous.json', self.selected)
            elif mode == 'pin_digest': identity['pins']['selected_sha256'] = 'wrong'
            else: review['accepted_reader_endpoint_binding_correction'] = False
            review['new_identity'] = identity
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.validate(review, identity=identity)

    def test_restart_requires_exact_succession_and_preserves_native_scientific_pins(self):
        self.run_watcher(self.corrected())
        with self.assertRaisesRegex(ValueError, 'reader_queue_identity_changed'):
            watch.Watcher(self.selected, {}, Path('.'), self.pins, self.accepted,
                          self.output, self.pause, workers=1).run()
        other = self.record('other_review.json', dict(self.review, annotation='different review'))
        with self.assertRaisesRegex(ValueError, 'unbound_branch'):
            self.corrected(reference=other).run()
        identity = copy.deepcopy(self.identity); identity['pins']['prompts']['rubric'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'scientific_pins_changed'):
            self.validate(dict(self.review, new_identity=identity), identity=identity)
