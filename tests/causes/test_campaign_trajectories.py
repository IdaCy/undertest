import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_trajectories as assembly
from tests.causes import test_campaign_reader_watch as fixture_module


class TrajectoryAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.ReaderWatcherTests('test_exact_bindings_boundary_and_full_prefix')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.job['allocation'].update(seed=1, continue_to_end=True)
        f.config['questions'] = [{'id': 'evaluation_presence', 'text': 'Synthetic question.'}]
        f.summary.update(early_workspace_status='captured', end_workspace_status='captured',
                         early_status='captured', calls=1, tool_results=0,
                         probes=[{'stage': 'early', 'instrument': 'evaluation_presence',
                                  'context_sha256': assembly.reader.digest(f.early),
                                  'question_sha256': assembly.reader.digest(f.config['questions'][0]),
                                  'technical_status': 'complete', 'answer': 'yes', 'reason': 'Synthetic retained reason.'}])
        for name, value in [('allocation', f.job['allocation']), ('model', f.config), ('summary', f.summary)]:
            (f.root / (name + '.json')).write_text(json.dumps(value))
        f.refresh_manifest()
        self.models = {'mi27': assembly.FrozenModel([f.job], f.config, 'frozen_jobs', 'frozen_model')}
        self.artifacts = {('mi27', 'fixture', stage): assembly.ReaderArtifacts(None, None, None)
                          for stage in ('early', 'end')}
        self.inputs = assembly.ReaderInputs({}, f.base / 'prompts', assembly.reader._SOURCE_SHA256,
                                            {'qualification': 'fixture'}, f.output, self.artifacts)

    def assemble(self, **kwargs):
        return assembly.assemble_trajectories(kwargs.pop('receipts', [self.fixture.receipt]),
            kwargs.pop('models', self.models), registered_roots=kwargs.pop('registered_roots', {'mi27': ['runs/mi27']}),
            snapshots_root=self.fixture.snapshots, reader_inputs=kwargs.pop('reader_inputs', self.inputs),
            awareness_types=('evaluation', 'simulation'), **kwargs)

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = assembly.reader.encoded(value)
        path.write_bytes(raw)
        return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def test_record_schema_censoring_and_explicit_pending_survive_later_files(self):
        before = copy.deepcopy((self.models, self.artifacts))
        value = self.assemble()
        row = value['records']['mi27']['fixture']
        self.assertEqual(set(row), {'run_id', 'model', 'task_id', 'variant_id', 'allocation', 'source',
            'summary_sha256', 'snapshot_manifest', 'end_status', 'early_status', 'actor_calls', 'tool_results',
            'context_sources', 'self', 'reader', 'reader_dispositions'})
        self.assertEqual(row['self']['early']['evaluation_presence']['label'], 'yes')
        self.assertEqual(row['reader_dispositions'], {'early': 'pending_reader', 'end': 'pending_reader'})
        self.assertEqual(row['end_status'], 'natural_completion')
        later = self.fixture.output / 'mi27/fixture/early/RESULT.json'
        later.parent.mkdir(parents=True)
        later.write_bytes(b'not part of this historical input cut')
        with patch.object(assembly, 'verify_reader_capture', side_effect=AssertionError('later output discovered')):
            self.assertEqual(self.assemble(), value)
        self.assertEqual((self.models, self.artifacts), before)

    def endpoint_fixture(self):
        f = self.fixture
        transition = f.endpoint_fixture()
        self.models['mi27'] = replace(self.models['mi27'], config=f.config,
            config_file_sha256=transition['value']['original_config']['sha256'])
        return transition, {transition['root']: transition['pair']}

    def test_endpoint_assembly_retains_primary_identity_and_secondary_backend_label(self):
        transition, mapping = self.endpoint_fixture()
        value = self.assemble(endpoint_transitions=mapping)
        row = value['records']['mi27']['fixture']
        self.assertEqual(row['allocation'], self.fixture.job['allocation'])
        self.assertEqual(row['frozen_model_sha256'], assembly.reader.digest(transition['original']))
        self.assertEqual(row['endpoint_transition']['captured_model_sha256'], assembly.reader.digest(transition['current']))
        self.assertEqual(row['endpoint_transition']['backend_label'], 'synthetic-backend')
        self.assertEqual(value['coverage']['mi27'], {'unobserved': [], 'quality_unavailable': []})
        with self.assertRaisesRegex(ValueError, 'frozen_identity_mismatch:model'):
            self.assemble()
        wrong = copy.deepcopy(self.models['mi27'].jobs)
        wrong[0]['allocation']['seed'] += 1
        with self.assertRaisesRegex(ValueError, 'frozen_scope_mismatch'):
            self.assemble(models={'mi27': replace(self.models['mi27'], jobs=wrong)}, endpoint_transitions=mapping)

    def test_endpoint_reader_result_and_missingness_require_retained_transition_binding(self):
        transition, mapping = self.endpoint_fixture();f = self.fixture
        binding, _ = assembly.watch.bind_snapshot(f.receipt, f.frozen, f.snapshots, self.inputs.binding_pins)
        binding_pin = self.save(f.output / 'mi27/fixture/BINDING.json', binding)
        missing_pin = self.save(f.output / 'mi27/fixture/early/TECHNICAL_MISSING.json',
            {'run_id': 'fixture', 'stage': 'early', 'status': 'technical_missing', 'admissible_labels': None,
             'replayed': False, 'binding_sha256': assembly.reader.digest(binding)})
        self.artifacts[('mi27', 'fixture', 'early')] = assembly.ReaderArtifacts(None, missing_pin, binding_pin)
        row = self.assemble(endpoint_transitions=mapping)['records']['mi27']['fixture']
        self.assertEqual(row['reader_dispositions']['early'], 'final_reader_technical_missing')
        result = {'instruments': {}}
        result_pin = self.save(f.output / 'mi27/fixture/end/RESULT.json', result)
        self.artifacts[('mi27', 'fixture', 'end')] = assembly.ReaderArtifacts(result_pin, None, None)
        with self.assertRaisesRegex(ValueError, 'endpoint_reader_binding_required'):
            self.assemble(endpoint_transitions=mapping)
        self.artifacts[('mi27', 'fixture', 'end')] = assembly.ReaderArtifacts(result_pin, None, binding_pin)
        with patch.object(assembly, 'verify_reader_capture', return_value=result):
            self.assertEqual(self.assemble(endpoint_transitions=mapping)['records']['mi27']['fixture']
                ['reader_dispositions']['end'], 'final_verified_reader_result')
        binding['endpoint_transition']['backend_label'] = 'wrong-backend'
        changed = self.save(f.output / 'mi27/fixture/BINDING.json', binding)
        self.artifacts[('mi27', 'fixture', 'early')] = assembly.ReaderArtifacts(None, None, None)
        self.artifacts[('mi27', 'fixture', 'end')] = assembly.ReaderArtifacts(result_pin, None, changed)
        with self.assertRaisesRegex(ValueError, 'endpoint_reader_binding_mismatch'):
            self.assemble(endpoint_transitions=mapping)

    def test_snapshot_conflicts_and_physical_root_alias_duplicate_refuse(self):
        other = dict(self.fixture.receipt, summary_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'conflicting_snapshot_identity'):
            self.assemble(receipts=[self.fixture.receipt, other])
        alias = dict(self.fixture.receipt, run='z_recovery/mi27/fixture')
        with self.assertRaisesRegex(ValueError, 'physical_assignment_present_in_multiple_output_roots'):
            self.assemble(receipts=[self.fixture.receipt, alias],
                          registered_roots={'mi27': ['runs/mi27', 'z_recovery/mi27']})

    def test_reader_namespaces_keep_assignment_custody_and_qualification_separate(self):
        f = self.fixture
        later_job = copy.deepcopy(f.job)
        later_job['allocation']['run_id'] = 'later'
        later_root = f.root.parent / 'later'
        later_root.mkdir()
        for path in f.root.iterdir():
            value = json.loads(path.read_bytes())
            if path.name in ('allocation.json', 'summary.json'):
                value['run_id'] = 'later'
            (later_root / path.name).write_text(json.dumps(value))
        files = []
        for path in later_root.iterdir():
            raw = path.read_bytes()
            files.append({'path': path.name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        summary_hash = next(row['sha256'] for row in files if row['path'] == 'summary.json')
        manifest = self.save(f.snapshots / 'later_manifest.json', {'runs': [
            {'run': 'runs/mi27/later', 'files': files, 'summary_sha256': summary_hash}]})
        receipt = {'run': 'runs/mi27/later', 'path': str(later_root), 'manifest': manifest['path'],
                   'manifest_sha256': manifest['sha256'], 'summary_sha256': summary_hash}
        result = {'instruments': {}}
        old_pin = self.save(f.output / 'mi27/fixture/early/RESULT.json', result)
        self.artifacts[('mi27', 'fixture', 'early')] = assembly.ReaderArtifacts(old_pin, None, None)
        new_base = f.base / 'reader_successor'
        new_pin = self.save(new_base / 'mi27/later/early/RESULT.json', result)
        new_artifacts = {('mi27', 'later', stage): assembly.ReaderArtifacts(
            new_pin if stage == 'early' else None, None, None) for stage in ('early', 'end')}
        successor = replace(self.inputs, output_base=new_base, artifacts=new_artifacts,
                            binding_pins={'qualification': 'successor'}, config={'reader': 'successor'})
        with patch.object(assembly, 'verify_reader_capture', return_value=result) as verify, \
                patch.object(assembly.watch, 'bind_snapshot', wraps=assembly.watch.bind_snapshot) as bind:
            actual = self.assemble(receipts=[f.receipt, receipt],
                models={'mi27': replace(self.models['mi27'], jobs=[f.job, later_job])},
                reader_inputs_by_assignment={('mi27', 'later'): successor})
        self.assertEqual(actual['records']['mi27']['fixture']['reader']['early']['path'], old_pin['path'])
        self.assertEqual(actual['records']['mi27']['later']['reader']['early']['path'], new_pin['path'])
        self.assertEqual([call.args[3] for call in bind.call_args_list],
                         [self.inputs.binding_pins, successor.binding_pins])
        self.assertEqual([call.args[4] for call in verify.call_args_list], [self.inputs.config, successor.config])
        self.assertEqual(actual['coverage']['mi27'], {'unobserved': [], 'quality_unavailable': []})

    def test_reader_override_never_borrows_another_namespaces_files_or_absences(self):
        f = self.fixture
        result_pin = self.save(f.output / 'mi27/fixture/early/RESULT.json', {'instruments': {}})
        self.artifacts[('mi27', 'fixture', 'early')] = assembly.ReaderArtifacts(result_pin, None, None)
        successor = replace(self.inputs, output_base=f.base / 'reader_successor', artifacts={})
        with self.assertRaisesRegex(ValueError, 'explicit_reader_artifact_or_absence_required'):
            self.assemble(reader_inputs_by_assignment={('mi27', 'fixture'): successor})
        successor = replace(successor, artifacts=self.artifacts)
        with self.assertRaisesRegex(ValueError, 'reader_artifact_identity_path_mismatch'):
            self.assemble(reader_inputs_by_assignment={('mi27', 'fixture'): successor})

    def test_reader_override_unknown_assignments_and_invalid_inputs_refuse(self):
        for value in ([], 0, '', False, [('mi27', 'fixture')]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reader_override_mapping_required'):
                self.assemble(reader_inputs_by_assignment=value)
        for key in [('other_model', 'fixture'), ('mi27', 'absent'), ('mi27',), 'fixture']:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'reader_override_assignment_unknown'):
                self.assemble(reader_inputs_by_assignment={key: self.inputs})
        with self.assertRaisesRegex(ValueError, 'reader_override_inputs_required'):
            self.assemble(reader_inputs_by_assignment={('mi27', 'fixture'): {}})

    def test_typed_frozen_identity_and_paired_context_drift_refuse(self):
        f = self.fixture
        changed = copy.deepcopy(f.job['allocation']);changed['seed'] = 1.0
        (f.root / 'allocation.json').write_text(json.dumps(changed));f.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'typed_frozen_identity_mismatch'):
            self.assemble()
        (f.root / 'allocation.json').write_text(json.dumps(f.job['allocation']))
        changed = copy.deepcopy(f.end);changed[len(f.initial)]['content'] = 'Another branch.'
        (f.root / 'end_context.json').write_text(json.dumps(changed))
        f.summary['actor_context_sha256'] = assembly.reader.digest(changed)
        (f.root / 'summary.json').write_text(json.dumps(f.summary));f.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'paired_prefix_context_mismatch'):
            self.assemble()

    def test_reader_technical_missing_and_self_missing_remain_nonnegative_unknowns(self):
        f = self.fixture
        binding, _ = assembly.watch.bind_snapshot(f.receipt, f.frozen, f.snapshots, self.inputs.binding_pins)
        binding_pin = self.save(f.output / 'mi27/fixture/BINDING.json', binding)
        missing_pin = self.save(f.output / 'mi27/fixture/early/TECHNICAL_MISSING.json',
            {'run_id': 'fixture', 'stage': 'early', 'status': 'technical_missing', 'admissible_labels': None,
             'replayed': False, 'binding_sha256': assembly.reader.digest(binding)})
        self.artifacts[('mi27', 'fixture', 'early')] = assembly.ReaderArtifacts(None, missing_pin, binding_pin)
        f.summary['probes'][0]['technical_status'] = 'truncated'
        (f.root / 'summary.json').write_text(json.dumps(f.summary));f.refresh_manifest()
        # The missing disposition must follow the exact summary it was bound to.
        with self.assertRaisesRegex(ValueError, 'reader_missing_disposition_binding_mismatch'):
            self.assemble()
        binding['snapshot_receipt'] = f.receipt
        binding_pin = self.save(f.output / 'mi27/fixture/BINDING.json', binding)
        missing = json.loads(Path(missing_pin['path']).read_bytes());missing['binding_sha256'] = assembly.reader.digest(binding)
        missing_pin = self.save(Path(missing_pin['path']), missing)
        self.artifacts[('mi27', 'fixture', 'early')] = assembly.ReaderArtifacts(None, missing_pin, binding_pin)
        row = self.assemble()['records']['mi27']['fixture']
        self.assertIsNone(row['self']['early']['evaluation_presence']['label'])
        self.assertEqual(row['self']['early']['evaluation_presence']['technical_status'], 'truncated')
        self.assertEqual(row['reader_dispositions']['early'], 'final_reader_technical_missing')
        self.assertEqual(row['reader'], {})

    def test_actor_stage_unavailable_and_quality_administrative_coverage_stay_separate(self):
        f = self.fixture;f.summary['end_status'] = 'technical_failure'
        (f.root / 'summary.json').write_text(json.dumps(f.summary));f.refresh_manifest()
        later = copy.deepcopy(f.job);later['allocation']['run_id'] = 'later'
        held = copy.deepcopy(f.job);held['allocation']['run_id'] = 'unissued_hold'
        models = {'mi27': replace(self.models['mi27'], jobs=[f.job, later, held])}
        annotation = {'status': 'supplied_quality_hold', 'hold_ids': ['hold_one'], 'raw_measurements_preserved': True}
        value = self.assemble(models=models, quality_held={'mi27': {'fixture': annotation}},
                              quality_unavailable={'mi27': ['unissued_hold']})
        self.assertEqual(value['coverage']['mi27'], {'unobserved': ['later'], 'quality_unavailable': ['unissued_hold']})
        row = value['records']['mi27']['fixture']
        self.assertEqual(row['reader_dispositions']['end'], 'final_actor_stage_unavailable')
        self.assertNotIn('end', row['context_sources'])
        self.assertEqual(row['intervention_quality'], annotation)
        with self.assertRaisesRegex(ValueError, 'quality_unissued_disposition_conflicts_with_observed_episode'):
            self.assemble(quality_unavailable={'mi27': ['fixture']})

    def test_reader_result_delegates_exact_context_and_preserves_semantic_categories(self):
        f = self.fixture
        result = {'instruments': {'presence': {'status': 'validated_partial_observation', 'judgments': {
            'evaluation': {'admissible_label': 'insufficient evidence to tell'},
            'simulation': {'admissible_label': None}}}}}
        result_pin = self.save(f.output / 'mi27/fixture/end/RESULT.json', result)
        self.artifacts[('mi27', 'fixture', 'end')] = assembly.ReaderArtifacts(result_pin, None, None)
        with patch.object(assembly, 'verify_reader_capture', return_value=result) as verify:
            row = self.assemble()['records']['mi27']['fixture']
        self.assertEqual(verify.call_args.args, (Path(result_pin['path']).parent, f.end, f.initial, 'full',
            self.inputs.config, self.inputs.prompts, self.inputs.expected_source_sha256))
        self.assertEqual(verify.call_args.kwargs, {'prefix_context': f.early})
        self.assertEqual(row['reader']['end']['instruments']['presence']['labels'],
                         {'evaluation': 'insufficient evidence to tell', 'simulation': None})
        self.assertEqual(row['reader_dispositions']['end'], 'final_verified_reader_result')
        # Identical reader bytes at another assignment path cannot substitute
        # for this run's capture, even if actor contexts happen to match.
        swapped = self.save(f.output / 'mi27/another_run/end/RESULT.json', result)
        self.artifacts[('mi27', 'fixture', 'end')] = assembly.ReaderArtifacts(swapped, None, None)
        with self.assertRaisesRegex(ValueError, 'reader_artifact_identity_path_mismatch'):
            self.assemble()
        self.artifacts[('mi27', 'fixture', 'end')] = assembly.ReaderArtifacts(result_pin, None, None)
        Path(result_pin['path']).write_bytes(b'changed historical aggregate')
        with self.assertRaisesRegex(ValueError, 'reader_artifact_size_mismatch'):
            self.assemble()
        self.save(Path(result_pin['path']), result)
        del self.artifacts[('mi27', 'fixture', 'early')]
        with self.assertRaisesRegex(ValueError, 'explicit_reader_artifact_or_absence_required'):
            self.assemble()


if __name__ == '__main__':
    unittest.main()
