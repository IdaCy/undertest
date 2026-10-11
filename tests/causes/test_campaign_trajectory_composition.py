import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_reader_freeze as freeze
from causes import campaign_trajectories as t
from tests.causes import test_campaign_trajectories as native_fixture


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return freeze.pin(path)


def job(run_id, stages=('early', 'end')):
    return {'spec': {'task_id': 'synthetic'}, 'variant': {'id': 'baseline'},
            'allocation': {'run_id': run_id, 'reader_stages': list(stages), 'seed': 7}}


class FrozenReaderInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'source'
        self.original.mkdir()
        self.destination = self.root / 'freeze'
        self.jobs = {'first': [job('shared'), job('absent', ('early',)), job('unselected', ())],
                     'second': [job('shared', ('early',))]}
        self.bindings = {}
        for model, jobs in self.jobs.items():
            self.bindings[model] = {
                'model': model, 'run_id': 'shared', 'frozen_job_sha256': t.reader.digest(jobs[0]),
                'reader_pins': {'qualification': {'name': model}},
                'snapshot_receipt': {'run': 'runs/' + model + '/shared', 'summary_sha256': model},
            }
            write(self.original / model / 'shared/BINDING.json', self.bindings[model])
        result = self.original / 'first/shared/early/RESULT.json'
        result.parent.mkdir()
        result.write_bytes(b'opaque result; not decoded by the metadata converter')
        slots = [slot for model, jobs in self.jobs.items() for slot in freeze.expected_slots(jobs, model)]
        freeze.freeze_reader_inputs(source_base=self.original, destination=self.destination, slots=slots,
                                    required_pins=[], provenance={'fixture': 'composition'})
        self.complete_path = self.destination / 'COMPLETE.json'
        self.complete = freeze.pin(self.complete_path)
        self.reader_config = {'reader': 'fixture'}
        self.kwargs = {'jobs_by_model': self.jobs, 'reader_config': self.reader_config,
                       'prompts': self.root / 'prompts', 'expected_reader_source_sha256': t.reader._SOURCE_SHA256,
                       'expected_freezer_source': freeze.pin(freeze.__file__)}

    def convert(self, **kwargs):
        return t.reader_inputs_from_freeze(self.complete, **{**self.kwargs, **kwargs})

    def rows(self):
        return json.loads((self.destination / 'READER_ARTIFACTS.json').read_bytes())

    def set_rows(self, rows, **fields):
        reference = write(self.destination / 'READER_ARTIFACTS.json', rows)
        complete = json.loads(self.complete_path.read_bytes())
        self.complete = write(self.complete_path, {**complete, 'reader_artifacts': reference, **fields})

    def set_binding(self, model, value):
        reference = write(self.destination / 'reader' / model / 'shared/BINDING.json', value)
        rows = self.rows()
        for row in rows:
            if row['model'] == model and row['run_id'] == 'shared':
                row['binding'] = reference
        self.set_rows(rows)

    def test_shared_freeze_keeps_model_keys_bindings_and_explicit_absences(self):
        inputs, receipts = self.convert()
        self.assertEqual(set(inputs), {('first', 'shared'), ('first', 'absent'), ('second', 'shared')})
        self.assertEqual(set(receipts), set(inputs))
        self.assertIsNone(receipts['first', 'absent'])
        for model in self.jobs:
            current = inputs[model, 'shared']
            self.assertEqual(current.binding_pins, self.bindings[model]['reader_pins'])
            self.assertEqual(receipts[model, 'shared'], self.bindings[model]['snapshot_receipt'])
            self.assertEqual(current.output_base, self.destination / 'reader')
            self.assertEqual(current.expected_source_sha256, t.reader._SOURCE_SHA256)
            self.assertEqual(len(current.artifacts), 4)
        self.assertEqual(inputs['first', 'absent'].artifacts['first', 'absent', 'early'],
                         t.ReaderArtifacts(None, None, None))

    def test_absences_do_not_discover_later_files(self):
        expected = self.convert()
        for base in (self.original, self.destination / 'reader'):
            write(base / 'first/absent/BINDING.json', {'later': 'not in the freeze'})
            write(base / 'first/absent/early/RESULT.json', {'later': 'not in the freeze'})
        self.assertEqual(self.convert(), expected)

    def test_distinct_freezer_and_reader_job_encoders_are_preserved(self):
        selected_job = self.jobs['first'][0]
        self.assertNotEqual(freeze.digest(selected_job), t.reader.digest(selected_job))
        self.convert()
        rows = self.rows()
        rows[0]['frozen_job_sha256'] = t.reader.digest(selected_job)
        self.set_rows(rows)
        with self.assertRaisesRegex(ValueError, 'frozen_reader_stage_identity_changed'):
            self.convert()
        rows[0]['frozen_job_sha256'] = freeze.digest(selected_job)
        self.set_rows(rows)
        self.set_binding('first', {**self.bindings['first'], 'frozen_job_sha256': freeze.digest(selected_job)})
        with self.assertRaisesRegex(ValueError, 'frozen_reader_binding_changed'):
            self.convert()

    def test_duplicate_foreign_or_missing_stages_refuse(self):
        original = self.rows()
        for rows in (original + [original[0]], original[1:],
                     [{**original[0], 'run_id': 'foreign'}, *original[1:]],
                     [{**original[0], 'stage': 'unknown'}, *original[1:]]):
            with self.subTest(rows=rows):
                self.set_rows(rows)
                with self.assertRaisesRegex(ValueError, 'frozen_reader_stage_identity_changed|reader_frozen_stage_cover'):
                    self.convert()

    def test_declared_slot_count_and_job_roster_are_checked(self):
        self.set_rows(self.rows(), slots=3)
        with self.assertRaisesRegex(ValueError, 'reader_frozen_stage_cover'):
            self.convert()
        self.set_rows(self.rows(), slots=True)
        with self.assertRaisesRegex(ValueError, 'reader_frozen_stage_cover'):
            self.convert()
        with self.assertRaisesRegex(ValueError, 'frozen_reader_job_sequence_required'):
            self.convert(jobs_by_model={'first': iter(self.jobs['first'])})
        with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_assignment'):
            self.convert(jobs_by_model={'first': [self.jobs['first'][0]] * 2})

    def test_different_bindings_between_stages_refuse(self):
        rows = self.rows()
        rows[1]['binding'] = None
        self.set_rows(rows)
        with self.assertRaisesRegex(ValueError, 'reader_binding_changed_between_stages'):
            self.convert()

    def test_present_binding_must_match_model_run_job_and_metadata(self):
        original = self.bindings['first']
        for field, value, reason in (('model', 'second', 'frozen_reader_binding_changed'),
                                     ('run_id', 'another', 'frozen_reader_binding_changed'),
                                     ('frozen_job_sha256', 'changed', 'frozen_reader_binding_changed'),
                                     ('reader_pins', None, 'frozen_reader_binding_metadata_required'),
                                     ('snapshot_receipt', None, 'frozen_reader_binding_metadata_required')):
            with self.subTest(field=field):
                self.set_binding('first', {**original, field: value})
                with self.assertRaisesRegex(ValueError, reason):
                    self.convert()

    def test_conflicting_or_unbound_published_dispositions_refuse(self):
        original = self.rows()
        rows = copy.deepcopy(original)
        rows[0]['technical_missing'] = {'path': str(self.destination / 'reader/first/shared/early/TECHNICAL_MISSING.json'),
                                         'bytes': 0, 'sha256': 'fixture'}
        self.set_rows(rows)
        with self.assertRaisesRegex(ValueError, 'reader_result_and_missing_conflict'):
            self.convert()
        rows = copy.deepcopy(original)
        rows[0]['binding'] = None
        self.set_rows(rows)
        with self.assertRaisesRegex(ValueError, 'published_reader_stage_without_binding'):
            self.convert()

    def test_source_schema_and_metadata_pins_are_checked(self):
        for reference in (self.complete, json.loads(self.complete_path.read_bytes())['reader_artifacts']):
            path = Path(reference['path'])
            raw = path.read_bytes()
            path.write_bytes(raw + b' ')
            with self.assertRaisesRegex(ValueError, 'reader_succession_pin_changed'):
                self.convert()
            path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'reader_freezer_source_mismatch'):
            self.convert(expected_freezer_source={**self.kwargs['expected_freezer_source'], 'sha256': 'other'})
        self.set_rows(self.rows(), schema='different')
        with self.assertRaisesRegex(ValueError, 'reader_freeze_schema'):
            self.convert()

    def test_binding_pin_drift_and_path_escape_refuse(self):
        path = self.destination / 'reader/first/shared/BINDING.json'
        raw = path.read_bytes()
        path.write_bytes(raw + b' ')
        with self.assertRaisesRegex(ValueError, 'reader_artifact_size_mismatch'):
            self.convert()
        path.write_bytes(raw)
        rows = self.rows()
        rows[0]['binding'] = write(self.root / 'outside.json', self.bindings['first'])
        self.set_rows(rows)
        with self.assertRaisesRegex(ValueError, 'snapshot_path_not_canonical_regular_storage'):
            self.convert()

    def test_artifact_metadata_must_belong_to_freeze_directory(self):
        complete = json.loads(self.complete_path.read_bytes())
        complete['reader_artifacts'] = write(self.root / 'elsewhere.json', self.rows())
        self.complete = write(self.complete_path, complete)
        with self.assertRaisesRegex(ValueError, 'reader_freeze_artifacts_path_mismatch'):
            self.convert()

    def test_returned_config_pins_and_receipts_do_not_mutate_caller_inputs(self):
        before = copy.deepcopy(self.kwargs)
        inputs, receipts = self.convert()
        inputs['first', 'shared'].config['reader'] = 'changed'
        inputs['first', 'shared'].binding_pins.clear()
        receipts['first', 'shared'].clear()
        self.assertEqual(self.kwargs, before)
        fresh, bound = self.convert()
        self.assertEqual(fresh['first', 'shared'].binding_pins, self.bindings['first']['reader_pins'])
        self.assertEqual(bound['first', 'shared'], self.bindings['first']['snapshot_receipt'])


def native_output(model, observed=(), unobserved=(), unavailable=(), issues=()):
    return {'records': {model: {rid: {'model': model, 'run_id': rid, 'self': {'technical_status': 'missing'}}
                               for rid in observed}},
            'coverage': {model: {'unobserved': list(unobserved), 'quality_unavailable': list(unavailable)}},
            'issues': list(issues)}


class TrajectoryOutputMergeTests(unittest.TestCase):
    def test_disjoint_groups_keep_missingness_issues_and_whole_roster_order(self):
        issue = {'run_id': 'b', 'issue': 'reader_unusable_context'}
        groups = [({'model': ['b', 'a']}, native_output('model', observed=['b'], unobserved=['a'], issues=[issue])),
                  ({'model': ['d', 'c']}, native_output('model', unobserved=['d'], unavailable=['c']))]
        before = copy.deepcopy(groups)
        with patch('builtins.open', side_effect=AssertionError('I/O forbidden')):
            result = t.merge_trajectory_outputs(groups, assignment_order={'model': ['d', 'c', 'b', 'a']})
        self.assertEqual(result['records']['model']['b'], before[0][1]['records']['model']['b'])
        self.assertEqual(result['coverage']['model'], {'unobserved': ['d', 'a'], 'quality_unavailable': ['c']})
        self.assertEqual(result['issues'], [issue])
        result['records']['model']['b']['self'].clear()
        result['issues'][0]['issue'] = 'changed'
        self.assertEqual(groups, before)

    def test_same_run_id_in_distinct_models_stays_distinct(self):
        groups = [({'one': ['same']}, native_output('one', observed=['same'])),
                  ({'two': ['same']}, native_output('two', unobserved=['same']))]
        result = t.merge_trajectory_outputs(groups, assignment_order={'one': ['same'], 'two': ['same']})
        self.assertEqual(set(result['records']['one']), {'same'})
        self.assertEqual(result['records']['two'], {})
        self.assertEqual(result['coverage']['two']['unobserved'], ['same'])

    def test_overlapping_group_rosters_refuse_even_if_all_unobserved(self):
        group = ({'model': ['a']}, native_output('model', unobserved=['a']))
        with self.assertRaisesRegex(ValueError, 'overlapping_or_unknown_trajectory_group'):
            t.merge_trajectory_outputs([group, copy.deepcopy(group)], assignment_order={'model': ['a']})

    def test_missing_group_and_foreign_group_refuse(self):
        group = ({'model': ['a']}, native_output('model', observed=['a']))
        with self.assertRaisesRegex(ValueError, 'whole_trajectory_roster_cover_required'):
            t.merge_trajectory_outputs([group], assignment_order={'model': ['a', 'b']})
        with self.assertRaisesRegex(ValueError, 'overlapping_or_unknown_trajectory_group'):
            t.merge_trajectory_outputs([group], assignment_order={'model': ['b']})

    def test_duplicate_unknown_and_cross_category_coverage_refuse(self):
        for value, reason in ((native_output('model', unobserved=['a', 'a']), 'duplicate_or_unknown'),
                              (native_output('model', unobserved=['b']), 'duplicate_or_unknown'),
                              (native_output('model', observed=['a'], unobserved=['a']), 'disjoint_complete'),
                              (native_output('model', unobserved=['a'], unavailable=['a']), 'disjoint_complete'),
                              (native_output('model'), 'disjoint_complete')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, reason):
                t.merge_trajectory_outputs([({'model': ['a']}, value)], assignment_order={'model': ['a']})

    def test_record_model_and_assignment_identity_cannot_be_substituted(self):
        for record in ({'model': 'another', 'run_id': 'a'}, {'model': 'model', 'run_id': 'b'}):
            value = native_output('model', observed=['a'])
            value['records']['model']['a'] = record
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, 'native_trajectory_record_identity'):
                t.merge_trajectory_outputs([({'model': ['a']}, value)], assignment_order={'model': ['a']})
        with self.assertRaisesRegex(ValueError, 'native_trajectory_record_identity'):
            t.merge_trajectory_outputs([({'model': ['a']}, native_output('model', observed=['foreign']))],
                                       assignment_order={'model': ['a']})

    def test_native_model_and_coverage_shapes_are_exact(self):
        original = native_output('model', unobserved=['a'])
        for field, replacement in (('records', {'other': {}}), ('coverage', {'other': {}}), ('issues', {})):
            value = {**original, field: replacement}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'native_trajectory_output_scope_mismatch'):
                t.merge_trajectory_outputs([({'model': ['a']}, value)], assignment_order={'model': ['a']})
        value = copy.deepcopy(original)
        value['coverage']['model']['inferred'] = []
        with self.assertRaisesRegex(ValueError, 'native_trajectory_coverage_shape'):
            t.merge_trajectory_outputs([({'model': ['a']}, value)], assignment_order={'model': ['a']})

    def test_invalid_whole_or_group_rosters_refuse(self):
        for roster in ({'model': ['a', 'a']}, {'model': 'a'}, {'model': ['../escape']}, {'../model': ['a']}):
            with self.subTest(roster=roster):
                with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_trajectory_assignment'):
                    t.merge_trajectory_outputs([], assignment_order=roster)
                with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_trajectory_assignment'):
                    t.merge_trajectory_outputs([(roster, native_output('model'))], assignment_order={'model': ['a']})

    def test_empty_roster_model_and_multiple_issue_lists_are_preserved(self):
        first = native_output('model', unobserved=['a'], issues=[{'order': 1}])
        second = native_output('model', unavailable=['b'], issues=[{'order': 2}])
        result = t.merge_trajectory_outputs([({'model': ['a']}, first), ({'model': ['b']}, second),
                                             ({'empty': []}, native_output('empty'))],
                                            assignment_order={'model': ['b', 'a'], 'empty': []})
        self.assertEqual(result['issues'], [{'order': 1}, {'order': 2}])
        self.assertEqual(result['coverage']['empty'], {'unobserved': [], 'quality_unavailable': []})


class NativeCompositionCompatibilityTests(unittest.TestCase):
    def test_real_freeze_to_native_assembly_and_disjoint_native_merge(self):
        case = native_fixture.TrajectoryAssemblyTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        f = case.fixture
        binding, _ = t.watch.bind_snapshot(f.receipt, f.frozen, f.snapshots, case.inputs.binding_pins)
        write(f.output / 'mi27/fixture/BINDING.json', binding)
        write(f.output / 'mi27/fixture/early/TECHNICAL_MISSING.json', {
            'run_id': 'fixture', 'stage': 'early', 'status': 'technical_missing',
            'admissible_labels': None, 'replayed': False, 'binding_sha256': t.reader.digest(binding)})
        destination = f.base / 'frozen_reader'
        freeze.freeze_reader_inputs(source_base=f.output, destination=destination,
                                    slots=freeze.expected_slots([f.job], 'mi27'), required_pins=[], provenance={})
        inputs, bound = t.reader_inputs_from_freeze(freeze.pin(destination / 'COMPLETE.json'),
            jobs_by_model={'mi27': [f.job]}, reader_config=case.inputs.config, prompts=case.inputs.prompts,
            expected_reader_source_sha256=case.inputs.expected_source_sha256,
            expected_freezer_source=freeze.pin(freeze.__file__))
        self.assertEqual(bound['mi27', 'fixture'], f.receipt)
        selection = t.select_reader_bound_receipts([f.receipt], {f.receipt['run']: f.receipt},
                                                  bound_receipts={f.receipt['run']: bound['mi27', 'fixture']})
        observed = case.assemble(receipts=selection['receipts'], reader_inputs_by_assignment=inputs)
        record = observed['records']['mi27']['fixture']
        self.assertEqual(record['reader_dispositions'], {'early': 'final_reader_technical_missing',
                                                         'end': 'pending_reader'})
        self.assertEqual(record['self']['early']['evaluation_presence']['label'], 'yes')
        self.assertEqual(observed['issues'], [])
        missing_jobs = [copy.deepcopy(f.job), copy.deepcopy(f.job)]
        for value, run_id in zip(missing_jobs, ('unobserved', 'unavailable')):
            value['allocation']['run_id'] = run_id
        missing = case.assemble(receipts=[], models={'mi27': replace(case.models['mi27'], jobs=missing_jobs)},
                                quality_unavailable={'mi27': ['unavailable']})
        merged = t.merge_trajectory_outputs([({'mi27': ['fixture']}, observed),
                                             ({'mi27': ['unobserved', 'unavailable']}, missing)],
                                            assignment_order={'mi27': ['unavailable', 'fixture', 'unobserved']})
        self.assertEqual(merged['records'], observed['records'])
        self.assertEqual(merged['coverage']['mi27'], {'unobserved': ['unobserved'],
                                                    'quality_unavailable': ['unavailable']})
        self.assertEqual(merged['issues'], [])


if __name__ == '__main__':
    unittest.main()
