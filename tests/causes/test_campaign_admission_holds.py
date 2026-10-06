import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import causes.campaign_dispatcher as module
from causes.campaign_dispatcher import Dispatcher, extension_block, sha
from test_campaign_dispatcher import job, finish


class AdmissionHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'runs'
        self.pause = self.root / 'PAUSE'
        self.config = {'model': 'model', 'endpoint': 'http://localhost:1', 'dispatcher': {'disk_floor_bytes': 0}}
        self.held = job('held_1', task='task_a')
        self.held['variant'].update(definition_sha256='d' * 64, stimulus_sha256='a' * 64)
        self.holds = self.root / 'holds.json'
        self.holds.write_text(json.dumps({'schema': 'caus.admission_holds.v1', 'holds': [
            {'hold_id': 'cell_a', 'task_id': 'task_a', 'definition_sha256': 'd' * 64, 'stimulus_sha256': 'a' * 64}]}))
        self.hold_sha = hashlib.sha256(self.holds.read_bytes()).hexdigest()

    def dispatcher(self, jobs, runner=None, **kwargs):
        return Dispatcher(jobs, self.config, self.output, pause_file=self.pause,
                          runner=runner or (lambda *args: self.fail('unexpected runner call')),
                          workers=1, max_full=0, poll_seconds=.005,
                          admission_holds=self.holds, admission_holds_sha256=self.hold_sha, **kwargs)

    def events(self):
        path = self.output / '.dispatcher/journal.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_until_pause(self, dispatcher):
        errors = []; results = []
        def target():
            try: results.append(dispatcher.run())
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=target, daemon=True); thread.start(); thread.join(2)
        if thread.is_alive():
            self.pause.touch(); thread.join(2); self.fail('bounded fixture did not pause')
        if errors: raise errors[0]
        return results[0]

    def test_all_repetitions_and_same_stimulus_alias_held_without_claim(self):
        alias = copy.deepcopy(self.held)
        alias['allocation']['run_id'] = 'held_alias_r2'
        alias['variant'].update(id='other_canonical_alias', definition_sha256='e' * 64)
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(allocation['run_id']); result = finish(spec, variant, config, allocation, output)
            self.pause.touch(); return result
        result = self.run_until_pause(self.dispatcher([self.held, alias, job('healthy')], runner))
        self.assertEqual(seen, ['healthy']); self.assertEqual(result['pending'], 2)
        self.assertFalse((self.output / 'held_1').exists()); self.assertFalse((self.output / 'held_alias_r2').exists())
        self.assertEqual({x['run_id'] for x in self.events() if x['event'] == 'admission_held'}, {'held_1', 'held_alias_r2'})
        self.assertEqual([x['run_id'] for x in self.events() if x['event'] == 'claimed'], ['healthy'])

    def test_unrelated_extensions_continue_with_complete_unchanged_blocks(self):
        prototypes = [copy.deepcopy(self.held), job('healthy_proto')]
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(copy.deepcopy(allocation)); result = finish(spec, variant, config, allocation, output)
            if len(seen) == 2: self.pause.touch()
            return result
        result = self.run_until_pause(self.dispatcher([self.held], runner, extension_prototypes=prototypes))
        self.assertEqual([x['block_index'] for x in seen], [0, 1]); self.assertEqual(result['pending'], 3)
        self.assertTrue(all(x['prototype_run_id'] == 'healthy_proto' for x in seen))
        for index in range(2):
            stored = json.loads((self.output / '.dispatcher/blocks' / f'{index:012d}.json').read_text())
            self.assertEqual(stored, extension_block(prototypes, self.config, index))
            self.assertEqual(len(stored['jobs']), 2)
        self.assertEqual(len([x for x in self.events() if x['event'] == 'claimed']), 2)

    def test_only_held_remaining_sleeps_without_creating_blocks(self):
        dispatcher = self.dispatcher([self.held], extension_prototypes=[self.held])
        sleeps = []
        original_sleep = time.sleep
        def bounded_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3: self.pause.touch()
            original_sleep(seconds)
        with patch.object(module.time, 'sleep', side_effect=bounded_sleep):
            result = self.run_until_pause(dispatcher)
        self.assertEqual(result['pending'], 1); self.assertEqual(len(sleeps), 3)
        self.assertFalse((self.output / '.dispatcher/blocks').exists())
        self.assertNotIn('claimed', [x['event'] for x in self.events()])

    def test_pin_rechecked_after_candidate_selection_before_claim(self):
        def free_bytes():
            self.holds.write_text(self.holds.read_text() + ' ')
            return 1
        with self.assertRaisesRegex(ValueError, 'pin_mismatch'):
            self.dispatcher([job('healthy')], free_bytes=free_bytes).run()
        self.assertNotIn('claimed', [x['event'] for x in self.events()])

    def test_final_hold_check_preserves_selected_assignment(self):
        healthy = job('healthy'); turn = {'after_selection': False}
        dispatcher = self.dispatcher([self.held, healthy])
        def matches(item):
            return ['late_fixture_hold'] if turn['after_selection'] and item['allocation']['run_id'] == 'held_1' else []
        dispatcher.admission_holds.matches = matches
        dispatcher.free_bytes = lambda: turn.update(after_selection=True) or 1
        def runner(spec, variant, config, allocation, output):
            self.assertEqual(allocation['run_id'], 'healthy')
            result = finish(spec, variant, config, allocation, output); self.pause.touch(); return result
        dispatcher.runner = runner
        self.assertEqual(self.run_until_pause(dispatcher)['pending'], 1)
        self.assertEqual([x['run_id'] for x in self.events() if x['event'] == 'claimed'], ['healthy'])

    def test_exact_definition_cannot_escape_with_changed_stimulus(self):
        changed = copy.deepcopy(self.held); changed['variant']['stimulus_sha256'] = 'b' * 64
        dispatcher = self.dispatcher([changed]); dispatcher.meta.mkdir()
        with self.assertRaisesRegex(ValueError, 'definition_stimulus_drift'): dispatcher.route_ready(changed)

    def predecessor(self, jobs, prototypes=None):
        self.pause.touch()
        dispatcher = self.dispatcher(jobs, extension_prototypes=prototypes)
        dispatcher.run()
        path = self.output / '.dispatcher/queue_identity.json'
        original = json.loads(path.read_text())
        original['execution_code']['dispatcher_sha256'] = module._SUCCESSION_PREDECESSOR
        path.write_text(json.dumps(original))
        return path, original

    def succession(self, path, original, **changes):
        current_code = copy.deepcopy(original['execution_code']); current_code['dispatcher_sha256'] = module._SOURCE_SHA
        review = self.root / 'review.json'
        review.write_text(json.dumps({'accepted_fixture': True}))
        receipt = {'schema': 'caus.dispatcher_code_succession.v1', 'output_root': str(self.output),
                   'predecessor_queue_identity': {'path': str(path), 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                   'successor_execution_code': current_code,
                   'admission_holds': {'path': str(self.holds), 'bytes': self.holds.stat().st_size, 'sha256': self.hold_sha},
                   'independent_review': {'path': str(review), 'bytes': review.stat().st_size, 'sha256': hashlib.sha256(review.read_bytes()).hexdigest()}}
        receipt.update(changes); target = self.root / ('succession_' + str(len(list(self.root.glob('succession_*')))) + '.json')
        target.write_text(json.dumps(receipt)); return {'code_succession': target, 'code_succession_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}

    def test_reviewed_successor_retains_predecessor_claims_blocks_and_quarantine(self):
        prototypes = [job('prototype')]
        path, original = self.predecessor([self.held], prototypes)
        dispatcher = self.dispatcher([self.held], extension_prototypes=prototypes)
        block = extension_block(prototypes, self.config, 0)
        module.save_new(dispatcher.meta / 'blocks/000000000000.json', block)
        dispatcher.event('claimed', run_id='held_1', job_sha256=sha(self.held))
        route = dispatcher.routes(self.held)[1]
        dispatcher.event('route_failure', route=route, consecutive_failures=2)
        dispatcher.event('route_quarantined', route=route, quarantine_id='retained')
        before = path.read_bytes(); block_before = (dispatcher.meta / 'blocks/000000000000.json').read_bytes()
        succession_args = self.succession(path, original)
        successor = self.dispatcher([self.held], extension_prototypes=prototypes, **succession_args)
        result = successor.run()
        self.assertEqual(result['status'], 'paused'); self.assertEqual(path.read_bytes(), before)
        self.assertEqual((dispatcher.meta / 'blocks/000000000000.json').read_bytes(), block_before)
        self.assertEqual(successor.failures[route], 2); self.assertEqual(successor.quarantines[route]['quarantine_id'], 'retained')
        self.assertEqual(len([x for x in self.events() if x['event'] == 'claimed']), 1)
        self.assertEqual(len(list((dispatcher.meta / 'code_successions').glob('*.json'))), 1)
        # A second restart accepts exactly the same explicit succession.
        self.dispatcher([self.held], extension_prototypes=prototypes, **succession_args).run()
        self.assertEqual(path.read_bytes(), before)

    def test_every_non_dispatcher_identity_drift_rejected(self):
        for field in ('jobs', 'config', 'prototypes', 'runner', 'sandbox', 'output', 'holds', 'successor'):
            with self.subTest(field=field):
                self.output = self.root / ('runs_' + field)
                jobs = [self.held]; prototypes = [job('proto')]
                path, original = self.predecessor(jobs, prototypes)
                changes = {}
                if field in ('runner', 'sandbox'):
                    name = 'campaign_' + field + '.py_sha256'; original['execution_code'][name] = 'f' * 64
                    path.write_text(json.dumps(original))
                if field == 'output': changes['output_root'] = str(self.root / 'different')
                if field == 'holds': changes['admission_holds'] = {'path': str(self.holds), 'bytes': 0, 'sha256': 'f' * 64}
                if field == 'successor': changes['successor_execution_code'] = {'dispatcher_sha256': 'f' * 64}
                args = self.succession(path, original, **changes)
                if field == 'jobs': jobs = [job('different')]
                if field == 'prototypes': prototypes = [job('different_proto')]
                config_before = self.config.copy()
                if field == 'config': self.config = {**self.config, 'model': 'changed'}
                try:
                    with self.assertRaisesRegex(RuntimeError, 'unapproved_queue_code_succession'):
                        self.dispatcher(jobs, extension_prototypes=prototypes, **args).run()
                finally: self.config = config_before

    def test_missing_succession_cannot_resume_old_queue(self):
        self.predecessor([self.held])
        with self.assertRaisesRegex(RuntimeError, 'queue_identity_changed'):
            self.dispatcher([self.held]).run()

    def test_fresh_queue_cannot_drop_or_change_bound_holds_on_restart(self):
        self.pause.touch(); self.dispatcher([self.held]).run()
        binding = self.output / '.dispatcher/admission_control.json'; original = binding.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'persisted_admission_control_changed'):
            Dispatcher([self.held], self.config, self.output, pause_file=self.pause).run()
        changed = self.root / 'other_holds.json'; changed.write_bytes(self.holds.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'persisted_admission_control_changed'):
            Dispatcher([self.held], self.config, self.output, pause_file=self.pause,
                       admission_holds=changed, admission_holds_sha256=self.hold_sha).run()
        self.assertEqual(binding.read_bytes(), original)
        self.assertNotIn('claimed', [x['event'] for x in self.events()])

    def test_hold_binding_precedes_new_queue_identity_publication(self):
        original = module.save_new
        def crash(path, value):
            if path.name == 'queue_identity.json': raise RuntimeError('fixture_crash')
            return original(path, value)
        with patch.object(module, 'save_new', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'fixture_crash'): self.dispatcher([self.held]).run()
        self.assertTrue((self.output / '.dispatcher/admission_control.json').is_file())
        with self.assertRaisesRegex(RuntimeError, 'persisted_admission_control_changed'):
            Dispatcher([self.held], self.config, self.output, pause_file=self.pause).run()

    def test_succession_cannot_authorize_new_unrelated_output(self):
        path, original = self.predecessor([self.held]); args = self.succession(path, original)
        self.output = self.root / 'unrelated'
        with self.assertRaisesRegex(RuntimeError, 'requires_existing_queue'):
            self.dispatcher([self.held], **args).run()

    def test_holds_symlink_and_missing_hash_rejected(self):
        alias = self.root / 'alias.json'; alias.symlink_to(self.holds)
        with self.assertRaisesRegex(ValueError, 'noncanonical'): module.AdmissionHolds(alias, self.hold_sha)
        with self.assertRaisesRegex(ValueError, 'pin_required'):
            Dispatcher([], self.config, self.output, admission_holds=self.holds)


if __name__ == '__main__': unittest.main()
