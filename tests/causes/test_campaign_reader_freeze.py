import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/campaign_reader_freeze.py'
spec = importlib.util.spec_from_file_location('reader_freezer', SOURCE)
freezer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freezer)


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / 'source'
        self.base.mkdir()
        self.output = self.root / 'freeze'
        self.slots = [{'job_index': 0, 'model': 'test_model', 'run_id': 'assignment',
                       'stage': 'early', 'frozen_job_sha256': 'fixture'}]
        self.binding = {'reader_source_base': str(self.base), 'slots': self.slots,
                        'required_pins': [], 'fixed_index_observed_at': 'earlier_fixed_cut'}
        self.reference_path = self.root / 'reference.json'
        self.reference_path.write_text('{}')
        self.reference = freezer.pin(self.reference_path)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, relative, data=b'opaque invalid JSON is never decoded'):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_freeze(self):
        freezer.freeze_reader_inputs(source_base=self.base, destination=self.output,
                                     slots=self.slots, required_pins=[self.reference],
                                     provenance={'fixture': self.reference})

    def test_full_whitelist_and_explicit_absences_without_semantic_decode(self):
        self.put('test_model/assignment/BINDING.json')
        self.put('test_model/assignment/early/RESULT.json')
        self.put('test_model/assignment/early/presence/response.raw', b'\xff\x00opaque')
        self.run_freeze()
        observations = [json.loads(line) for line in (self.output / 'OBSERVED.jsonl').read_text().splitlines()]
        self.assertEqual(len(observations), 26)
        self.assertEqual(sum(row['source'] is not None for row in observations), 3)
        self.assertEqual({row['relative_path'] for row in observations}, set(freezer.relative_paths(self.slots)))
        self.assertEqual((self.output / 'reader/test_model/assignment/early/presence/response.raw').read_bytes(), b'\xff\x00opaque')
        complete = json.loads((self.output / 'COMPLETE.json').read_text())
        self.assertFalse(complete['raw_verification_performed'])
        artifacts = json.loads((self.output / 'READER_ARTIFACTS.json').read_text())
        self.assertIsNone(artifacts[0]['technical_missing'])
        self.assertEqual(artifacts[0]['result']['path'], str(self.output / 'reader/test_model/assignment/early/RESULT.json'))

    def test_path_expansion_uses_one_assignment_binding_for_two_stages(self):
        slots = self.slots + [{**self.slots[0], 'stage': 'end'}]
        paths = freezer.relative_paths(slots)
        self.assertEqual(len(paths), 51)
        self.assertEqual(paths.count('test_model/assignment/BINDING.json'), 1)
        self.assertIn('test_model/assignment/end/leans_towards_or_favors/parsed.json', paths)

    def test_symlink_file_refuses_and_preserves_partial_evidence(self):
        stage = self.base / 'test_model/assignment/early'
        stage.mkdir(parents=True)
        (stage / 'RESULT.json').symlink_to(self.reference_path)
        with self.assertRaisesRegex(ValueError, 'symlink_refused'):
            self.run_freeze()
        self.assertTrue((self.output / 'FAILED.json').exists())
        self.assertFalse((self.output / 'COMPLETE.json').exists())

    def test_symlink_ancestor_refuses(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.base / 'test_model').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink_refused'):
            self.run_freeze()

    def test_changed_bytes_during_final_pass_refuse_without_retry(self):
        path = self.put('test_model/assignment/BINDING.json', b'first')
        original, changed = freezer.observe, []
        def observed(source, destination=None):
            if Path(source) == path and destination is None and not changed:
                path.write_bytes(b'second')
                changed.append(True)
            return original(source, destination)
        with patch.object(freezer, 'observe', side_effect=observed):
            with self.assertRaisesRegex(ValueError, 'reader_changed_during_freeze'):
                self.run_freeze()
        self.assertEqual((self.output / 'reader/test_model/assignment/BINDING.json').read_bytes(), b'first')
        self.assertTrue((self.output / 'FAILED.json').exists())
        with self.assertRaisesRegex(ValueError, 'fresh_exclusive_freeze_required'):
            self.run_freeze()

    def test_absence_becoming_present_is_not_silently_pending(self):
        original, created = freezer.observe, []
        target = self.base / 'test_model/assignment/early/RESULT.json'
        def observed(source, destination=None):
            if Path(source) == target and destination is None and not created:
                self.put('test_model/assignment/early/RESULT.json')
                created.append(True)
            return original(source, destination)
        with patch.object(freezer, 'observe', side_effect=observed):
            with self.assertRaisesRegex(ValueError, 'reader_changed_during_freeze'):
                self.run_freeze()

    def test_published_result_missing_binding_refuses_after_retaining_bytes(self):
        self.put('test_model/assignment/early/RESULT.json')
        with self.assertRaisesRegex(ValueError, 'published_stage_without_binding'):
            self.run_freeze()
        self.assertTrue((self.output / 'reader/test_model/assignment/early/RESULT.json').exists())

    def test_result_and_missing_conflict_refuses(self):
        for relative in ('BINDING.json', 'early/RESULT.json', 'early/TECHNICAL_MISSING.json'):
            self.put('test_model/assignment/' + relative)
        with self.assertRaisesRegex(ValueError, 'result_and_technical_missing_conflict'):
            self.run_freeze()

    def test_duplicate_or_escape_frozen_ids_and_stages_refuse(self):
        job = {'allocation': {'run_id': 'valid', 'reader_stages': ['early']}}
        self.assertEqual(len(freezer.expected_slots([job], 'test_model')), 1)
        with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_assignment'):
            freezer.expected_slots([job, job], 'test_model')
        for rid in ('../escape', '.', '/absolute'):
            with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_assignment'):
                freezer.expected_slots([{'allocation': {'run_id': rid, 'reader_stages': []}}], 'test_model')
        with self.assertRaisesRegex(ValueError, 'invalid_reader_stage_roster'):
            freezer.expected_slots([{'allocation': {'run_id': 'valid', 'reader_stages': ['early', 'early']}}], 'test_model')

    def test_existing_destination_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / 'sentinel'
        sentinel.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'fresh_exclusive_freeze_required'):
            self.run_freeze()
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_scope_escape_duplicate_slots_and_overlap_refuse_before_copy(self):
        for changed in ({'model': '../other'}, {'run_id': '../outside'}, {'stage': 'unknown'}):
            with self.assertRaisesRegex(ValueError, 'invalid_slot_identity'):
                freezer.relative_paths([{**self.slots[0], **changed}])
        with self.assertRaisesRegex(ValueError, 'duplicate_slot_identity'):
            freezer.relative_paths(self.slots * 2)
        with self.assertRaisesRegex(ValueError, 'source_and_destination_must_be_disjoint'):
            freezer.freeze_reader_inputs(source_base=self.base, destination=self.base / 'nested',
                slots=self.slots, required_pins=[], provenance={})
        self.assertFalse((self.base / 'nested').exists())

    def test_immutable_input_drift_refuses_before_destination_creation(self):
        self.reference_path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'immutable_pin_mismatch'):
            self.run_freeze()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
