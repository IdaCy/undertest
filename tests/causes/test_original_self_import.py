import json
from pathlib import Path
import socket
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from causes import campaign_original_self_import as worker


class OriginalSelfImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def save(self, name, value):
        return worker.save(self.root / name, value)

    def test_changed_pin_duplicate_json_and_nonfinite_refuse(self):
        path = self.root / 'input.json'
        for body, reason in [(b'{"x":1,"x":2}', 'duplicate_json_key'),
                             (b'{"x":NaN}', 'nonfinite_json')]:
            path.write_bytes(body)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                worker.Inputs().checked(worker.pin(path))
        ref = worker.pin(path)
        path.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'changed_input'):
            worker.Inputs().checked(ref)

    def test_acceptance_join_is_json_exact(self):
        ref = self.save('peer.json', dict(accepted_example=True, findings=[], count=True))
        with self.assertRaisesRegex(ValueError, 'accepted_example'):
            worker.Inputs().accepted(ref, 'accepted_example', count=1)

    def test_unauthorized_import_never_derives_or_writes(self):
        req = self.save('request.json', dict(schema='causes.original_self_import_request.v1',
                  source=worker.pin(worker.__file__), runtime_authorized=False))
        auth = self.save('authority.json', dict(accepted_original_self_import_execution=True,
            source=worker.pin(worker.__file__), request=req, maximum_invocations=1, automatic_retry=False))
        with patch.object(worker, 'derive', side_effect=AssertionError('reached derive')):
            with self.assertRaisesRegex(ValueError, 'import_not_authorized'):
                worker.run(req, auth)

    def previous_fixture(self, duplicate=False, wrong_count=False):
        source = worker.pin(worker.__file__)
        assignments = self.save('assignments.json', dict(epochs={'old': 'historical'},
                                                       readout_names=list(worker.batch.READOUTS)))
        segments = [dict(kind='custody', segment={'tag': 'base'}, epoch_policy='required', group=None),
                    dict(kind='slots', segment={'tag': 'later'}, epoch_policy='required', group=None)]
        manifest = self.save('manifest.json', dict(assignments=assignments,
                           segments=[x['segment'] for x in segments], physical_measurements=2))
        coverage = self.save('coverage.json', {})
        result = dict(source=source, manifest=manifest, coverage=coverage, assignments=assignments,
                      public_merge_source=source, terminal_slots=3 if wrong_count else 2)
        actual = self.save('previous.json', result)
        peer = self.save('previous_peer.json', dict(accepted_previous=True, findings=[], actual=actual,
                       **{key: result[key] for key in ('source','manifest','coverage','assignments','public_merge_source')}))
        bindings = dict(previous=dict(result=actual, review=peer, acceptance_flag='accepted_previous'),
                        assignments=assignments, sources={'reuse': source}, previous_segments=segments)
        sentinel = object()
        calls = []
        def load_segment(descriptor, *args, **kwargs):
            calls.append(descriptor['segment']['tag'])
            name = 'self_evaluation_presence' if duplicate or len(calls) == 1 else 'self_evaluation_strongly_favors'
            return [dict(run_id='old', readout=name, status='missing', label=None,
                         reason='original failure', evidence={})], {('old', name)}, []
        module = types.SimpleNamespace(record=sentinel, load_reuse=lambda ref: object(), load_segment=load_segment)
        return bindings, worker.Inputs().checked(assignments), module, sentinel, calls

    def test_every_previous_segment_and_missing_slot_is_occupied(self):
        bindings, assignments, module, sentinel, calls = self.previous_fixture()
        keys = worker.occupied_keys(bindings, assignments, module, worker.Inputs())
        self.assertEqual(calls, ['base', 'later'])
        self.assertEqual(len(keys), 2)
        self.assertIs(module.record, sentinel)

    def test_prior_duplicate_or_count_drift_refuses_and_restores_loader(self):
        for options, reason in [(dict(duplicate=True), 'previous_duplicate_slot'),
                                (dict(wrong_count=True), 'previous_occupied_count')]:
            with self.subTest(options=options):
                self.tmp.cleanup();self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
                self.root = Path(self.tmp.name)
                bindings, assignments, module, sentinel, _ = self.previous_fixture(**options)
                with self.assertRaisesRegex(ValueError, reason):
                    worker.occupied_keys(bindings, assignments, module, worker.Inputs())
                self.assertIs(module.record, sentinel)

    def test_old_manifest_cannot_drop_a_segment(self):
        bindings, assignments, module, sentinel, _ = self.previous_fixture()
        bindings['previous_segments'].pop()
        with self.assertRaisesRegex(ValueError, 'previous_segments_changed'):
            worker.occupied_keys(bindings, assignments, module, worker.Inputs())
        self.assertIs(module.record, sentinel)

    def test_previous_helper_allows_only_verified_same_byte_copy(self):
        bindings, assignments, module, sentinel, _ = self.previous_fixture()
        copy = self.root / 'preserved_reuse.py'
        copy.write_bytes(Path(worker.__file__).read_bytes())
        bindings['sources']['reuse'] = worker.pin(copy)
        self.assertEqual(len(worker.occupied_keys(bindings, assignments, module, worker.Inputs())), 2)
        copy.write_bytes(copy.read_bytes() + b'\n')
        bindings['sources']['reuse'] = worker.pin(copy)
        with self.assertRaisesRegex(ValueError, 'previous_identity'):
            worker.occupied_keys(bindings, assignments, module, worker.Inputs())
        self.assertIs(module.record, sentinel)

    def guarded_fixture(self):
        guards = []
        class Guard:
            def __init__(self, files, output, sources, directories):
                self.active = True;self.finished = False;guards.append(self)
            def audit(self, event, args):
                return None
            def finish(self):
                self.finished = True
        ref = worker.pin(worker.__file__)
        source_names = worker.NATIVE_NAMES | {'batch','feed','reader_feed','reuse','snapshot'}
        b = dict(source=ref, sources={k: ref for k in source_names}, assignments=ref,
                 previous={'result': ref}, self_cut=ref, self_cut_review=ref,
                 reader_cut=ref, reader_cut_review=ref)
        m = {k: object() for k in worker.NATIVE_NAMES};m['reader'] = types.SimpleNamespace(ReadGuard=Guard)
        state = dict(pending=False, bindings=b, modules=m, prepared=dict(files=[], read_directories=[], groups=[]),
                     selection={'selected_run_ids':['r']}, old_keys=set(), jobs={},
                     assignments=dict(epochs={'r':'prospective'}, readout_names=list(worker.batch.READOUTS)))
        result = dict(selected_run_ids=['r'], slots=[dict(run_id='r', readout=name, status='missing', label=None,
                     reason='native_probe_failure', evidence={}, epoch='prospective') for name in worker.batch.READOUTS],
                     counts=dict(occupied=3, observed=0, missing=3), host_calls=0, model_calls=0, analysis_performed=False)
        return state, result, guards

    def test_saved_guard_finishes_and_missing_groups_are_immutable_slots(self):
        state, result, guards = self.guarded_fixture()
        with patch.object(worker.batch, 'bind_batch', return_value=result):
            ref = worker.guarded_bind(state, self.root/'output')
        value = worker.Inputs().checked(ref)
        self.assertEqual(value['counts']['missing'], 3)
        self.assertEqual(value['selected_run_ids'], ['r'])
        self.assertEqual(len(worker.Inputs().checked(value['slots'])), 3)
        self.assertTrue(guards[0].finished)
        self.assertFalse(guards[0].active)

    def test_external_action_refuses_and_guard_is_restored(self):
        state, _, guards = self.guarded_fixture()
        original = subprocess.run
        def attempt(*args, **kwargs):
            subprocess.run(['must-not-run'])
        with patch.object(worker.batch, 'bind_batch', side_effect=attempt):
            with self.assertRaisesRegex(ValueError, 'external_action_forbidden'):
                worker.guarded_bind(state, self.root/'output')
        self.assertIs(subprocess.run, original)
        self.assertFalse(guards[0].active)
        self.assertTrue((self.root/'output/FAILED.v1.json').exists())
        self.assertFalse((self.root/'output/RESULT.v1.json').exists())

    def test_occupied_output_refuses_without_replacing_the_old_key(self):
        state, result, guards = self.guarded_fixture()
        state['old_keys'] = {('r', worker.batch.READOUTS[0])}
        with patch.object(worker.batch, 'bind_batch', return_value=result):
            with self.assertRaisesRegex(ValueError, 'occupied_slot_replay'):
                worker.guarded_bind(state, self.root/'output')
        self.assertFalse(guards[0].active)
        self.assertFalse((self.root/'output/SLOTS.v1.json').exists())

    def test_pending_copy_cannot_create_output_or_native_guard(self):
        state, _, guards = self.guarded_fixture()
        state['pending'] = True
        with self.assertRaisesRegex(ValueError, 'paired_copy_batch_pending'):
            worker.guarded_bind(state, self.root/'output')
        self.assertEqual(guards, [])
        self.assertFalse((self.root/'output').exists())


if __name__ == '__main__':
    unittest.main()
