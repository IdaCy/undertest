import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from causes import campaign_reader_watch as watch


class ReaderWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.snapshots = self.base / 'snapshots'
        self.snapshots.mkdir()
        self.root = self.snapshots / 'attempt/data/runs/mi27/fixture'
        self.root.mkdir(parents=True)
        self.output = self.base / 'reader'
        self.pause = self.base / 'PAUSE'
        self.accepted = self.snapshots / 'accepted.jsonl'
        self.job = {'spec': {'workspace': '/workspace/project', 'task_id': 'task'},
                    'variant': {'id': 'canonical', 'request': 'Repair the bug.',
                                'history': [{'role': 'assistant', 'content': 'Provided assistant history.'}]},
                    'allocation': {'run_id': 'fixture', 'reader_stages': ['early', 'end']}}
        self.config = {'system': 'Work in {workspace}.'}
        self.initial = [{'role': 'system', 'content': 'Work in /workspace/project.'},
                        *self.job['variant']['history'], {'role': 'user', 'content': 'Repair the bug.'}]
        self.early = self.initial + [{'role': 'assistant', 'content': 'I will inspect it.', 'reasoning': 'Consider the failure.'}]
        self.end = self.early + [{'role': 'assistant', 'content': 'Done.', 'reasoning': 'The repair is complete.'}]
        self.summary = {'run_id': 'fixture', 'end_status': 'natural_completion',
                        'sandbox_closure': {'state': 'closed', 'overlay_mounted': False},
                        'early_context_sha256': watch.reader.digest(self.early),
                        'actor_context_sha256': watch.reader.digest(self.end)}
        self.frozen = {'model': 'mi27', 'job': self.job, 'config': self.config,
                       'jobs_file_sha256': 'frozen_jobs', 'model_file_sha256': 'frozen_model'}
        files = {'allocation.json': self.job['allocation'], 'spec.json': self.job['spec'],
                 'variant.json': self.job['variant'], 'model.json': self.config, 'summary.json': self.summary,
                 'initial_context.json': self.initial, 'early_context.json': self.early, 'end_context.json': self.end}
        for name, value in files.items():
            (self.root / name).write_text(json.dumps(value))
        self.manifest = self.snapshots / 'attempt/MANIFEST.json'
        self.refresh_manifest()

    def refresh_manifest(self):
        rows = [{'path': path.name, 'bytes': path.stat().st_size, 'sha256': watch.file_sha(path)} for path in self.root.iterdir()]
        self.manifest.write_text(json.dumps({'runs': [{'run': 'runs/mi27/fixture', 'files': rows,
                                                       'summary_sha256': watch.file_sha(self.root / 'summary.json')}]}))
        self.receipt = {'run': 'runs/mi27/fixture', 'path': str(self.root), 'manifest': str(self.manifest),
                        'manifest_sha256': watch.file_sha(self.manifest), 'summary_sha256': watch.file_sha(self.root / 'summary.json')}
        self.accepted.write_text(json.dumps(self.receipt) + '\n')

    def watcher(self, runner):
        return watch.Watcher({'fixture': self.frozen}, {}, Path('.'), {'pin': 'qualified'}, self.accepted,
                             self.output, self.pause, context_runner=runner, workers=4, poll_seconds=0.005)

    def run_watcher(self, watcher):
        errors = []
        def target():
            try:
                watcher.run()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(3)
        if thread.is_alive():
            self.pause.touch()
            thread.join(3)
            self.fail('watcher fixture failed to reach pause')
        if errors:
            raise errors[0]

    def test_exact_bindings_boundary_and_full_prefix(self):
        binding, loaded = watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {'pin': 'qualified'})
        self.assertEqual(binding['source_files']['summary.json']['sha256'], self.receipt['summary_sha256'])
        messages, initial, prefix, phase = watch.stage_context('end', loaded)
        self.assertEqual(messages, self.end)
        self.assertEqual(initial, self.initial)
        self.assertEqual(prefix, self.early)
        self.assertEqual(phase, 'full')
        altered = copy.deepcopy(loaded)
        altered['end_context.json'][len(self.initial)]['content'] = 'A different branch.'
        altered['summary.json']['actor_context_sha256'] = watch.reader.digest(altered['end_context.json'])
        with self.assertRaisesRegex(ValueError, 'paired_prefix_context_mismatch'):
            watch.stage_context('end', altered)

    def endpoint_fixture(self):
        from tests.causes.test_campaign_endpoint_transition import fixture
        self.job['allocation']['continue_to_end'] = True
        f = fixture(self.base, self.job, self.config, root='runs/mi27')
        self.config = f['original']
        self.frozen.update(config=self.config, model_file_sha256=f['value']['original_config']['sha256'],
                           endpoint_transitions={f['root']: f['pair']})
        (self.root / 'allocation.json').write_text(json.dumps(self.job['allocation']))
        (self.root / 'model.json').write_text(json.dumps(f['current']))
        self.refresh_manifest()
        return f

    def test_endpoint_binding_keeps_frozen_identity_and_captured_backend_separate(self):
        f = self.endpoint_fixture()
        binding, loaded = watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {})
        self.assertEqual(binding['frozen_model_sha256'], watch.reader.digest(f['original']))
        self.assertEqual(binding['model_file_sha256'], f['value']['original_config']['sha256'])
        self.assertEqual(binding['endpoint_transition']['captured_model_sha256'], watch.reader.digest(f['current']))
        self.assertEqual(binding['endpoint_transition']['backend_label'], 'synthetic-backend')
        self.assertEqual(loaded['model.json'], f['current'])
        changed = copy.deepcopy(f['current']);changed['system'] = 'Changed request'
        (self.root / 'model.json').write_text(json.dumps(changed));self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'frozen_identity_mismatch:model'):
            watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {})

    def test_endpoint_binding_rejects_typed_allocation_change_before_reader_call(self):
        self.endpoint_fixture()
        changed = dict(self.job['allocation'], continue_to_end=1)
        (self.root / 'allocation.json').write_text(json.dumps(changed));self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'typed_endpoint_identity_mismatch'):
            watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {})

    def test_endpoint_binding_requires_mapping_and_exact_output_root(self):
        f = self.endpoint_fixture()
        absent = dict(self.frozen);absent.pop('endpoint_transitions')
        with self.assertRaisesRegex(ValueError, 'frozen_identity_mismatch:model'):
            watch.bind_snapshot(self.receipt, absent, self.snapshots, {})
        self.frozen['endpoint_transitions'] = {'runs/mi27': f['accepted'](dict(f['value'],
            output_root=str(self.base / 'runs/different/mi27')))}
        with self.assertRaisesRegex(ValueError, 'frozen_scope_mismatch'):
            watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {})

    def test_snapshot_mutation_and_frozen_variant_mismatch_are_rejected(self):
        (self.root / 'early_context.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'source_file_hash_mismatch'):
            watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {})
        self.refresh_manifest()
        altered = copy.deepcopy(self.frozen)
        altered['job']['variant']['request'] = 'Different task.'
        with self.assertRaisesRegex(ValueError, 'frozen_identity_mismatch'):
            watch.bind_snapshot(self.receipt, altered, self.snapshots, {})

    def test_only_frozen_stages_execute_once_and_restart_never_replays(self):
        calls, mutex = [], threading.Lock()
        def runner(client, config, prompts, messages, boundary, destination, **kwargs):
            with mutex:
                calls.append((str(destination), messages, boundary, kwargs))
                if len(calls) == 2:
                    self.pause.touch()
            destination.mkdir(parents=True, exist_ok=False)
            result = {'context_sha256': watch.reader.digest(messages), 'instruments': {'presence': {'status': 'validated'}}}
            watch.save(destination / 'RESULT.json', result)
            return result
        self.run_watcher(self.watcher(runner))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[2] == len(self.initial) for call in calls))
        self.assertTrue((self.output / 'mi27/fixture/BINDING.json').is_file())
        self.assertTrue((self.output / 'mi27/fixture/early/RESULT.json').is_file())
        self.assertTrue((self.output / 'mi27/fixture/end/RESULT.json').is_file())
        self.run_watcher(self.watcher(lambda *args, **kwargs: self.fail('reader stage replayed')))

    def test_existing_partial_stage_is_never_replayed(self):
        partial = self.output / 'mi27/fixture/early'
        partial.mkdir(parents=True)
        (partial / 'attempt.raw').write_bytes(b'preserve')
        calls = []
        def runner(client, config, prompts, messages, boundary, destination, **kwargs):
            calls.append(destination.name)
            self.pause.touch()
            destination.mkdir(parents=True)
            result = {'instruments': {}}
            watch.save(destination / 'RESULT.json', result)
            return result
        self.run_watcher(self.watcher(runner))
        self.assertEqual(calls, ['end'])
        self.assertEqual((partial / 'attempt.raw').read_bytes(), b'preserve')
        rows = watch.jsonlines(self.output / '.watch/journal.jsonl', strict=True)
        self.assertTrue(any(row['event'] == 'existing_reader_output_not_replayed' for row in rows))

    def test_missing_stage_is_explicit_technical_missing_without_api(self):
        binding, loaded = watch.bind_snapshot(self.receipt, self.frozen, self.snapshots, {})
        del loaded['end_context.json']
        watcher = self.watcher(lambda *args, **kwargs: self.fail('missing stage invoked API'))
        output = self.output / 'mi27/fixture/end'
        result = watcher.execute(self.frozen, 'end', output, binding, loaded, None)
        self.assertEqual(result['status'], 'technical_missing')
        self.assertIsNone(result['admissible_labels'])
        self.assertTrue((output / 'TECHNICAL_MISSING.json').exists())

    def test_torn_acceptance_waits_and_torn_claim_journal_fails_closed(self):
        self.accepted.write_text(json.dumps(self.receipt))
        self.assertEqual(watch.jsonlines(self.accepted), [])
        with self.assertRaisesRegex(ValueError, 'torn_reader_journal'):
            watch.jsonlines(self.accepted, strict=True)

    def test_lexical_parent_escape_and_symlink_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'snapshot_path_not_canonical'):
            watch.confined(self.snapshots / '../outside.json', self.snapshots)
        link = self.snapshots / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'snapshot_path_not_canonical'):
            watch.confined(link / 'summary.json', self.snapshots)

    def test_recurring_api_failure_quarantines_but_observational_missingness_does_not(self):
        watcher = self.watcher(lambda *args, **kwargs: None)
        missing_observation = {'instruments': {'presence': {'status': 'technical_missing',
                                                          'technical_reason': 'no_generated_actor_text_captured'}}}
        for index in range(4):
            watcher.observe_result(str(index), missing_observation)
        self.assertEqual(watcher.failures, 0)
        failed = {'instruments': {'presence': {'status': 'technical_missing', 'technical_reason': 'APITimeoutError'}}}
        for index in range(3):
            watcher.observe_result(str(index), failed)
        self.assertTrue(watcher.route_blocked())
        quarantine = watcher.quarantine
        path = Path(quarantine['required_resolution'])
        watch.save(path, {'status': 'qualified', 'quarantine_id': quarantine['quarantine_id'],
                          'qualification_evidence': 'retained_api_transport_smoke.json'})
        self.assertFalse(watcher.route_blocked())
        self.assertEqual(watcher.failures, 0)

    def test_explicit_model_roster_and_qualified_input_resolution(self):
        def write(name, value):
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
            return {'path': name, 'sha256': watch.file_sha(path)}
        artifacts = []
        for model in ('first', 'second'):
            job = copy.deepcopy(self.job)
            job['allocation'].update(run_id=model, reader_thresholds=list(watch.reader.INSTRUMENTS))
            artifacts.extend([write(model + '_jobs.json', [job]), write(model + '_config.json', self.config)])
        protocol = self.base / 'protocol.json'
        protocol.write_text(json.dumps({'artifacts': artifacts}))
        qualified = [write('qualified/reader_config.json', {'timeout_seconds': 180})]
        for name in ('rubric', *watch.reader.INSTRUMENTS):
            qualified.append(write('qualified/prompts/' + name + '.txt', 'fixture prompt'))
        milestone = self.base / 'qualification.json'
        milestone.write_text(json.dumps({'source_sha256': watch.file_sha(watch.reader.__file__), 'pins': qualified}))
        kwargs = dict(models=['second'], pin_base=self.base,
                      checker_config_path=self.base / 'qualified/reader_config.json',
                      prompts_path=self.base / 'qualified/prompts')
        selected, config, prompts, pins = watch.load_frozen(protocol, milestone, **kwargs)
        self.assertEqual(list(selected), ['second'])
        self.assertEqual(selected['second']['model'], 'second')
        self.assertEqual(config, {'timeout_seconds': 180})
        self.assertEqual(prompts, kwargs['prompts_path'])
        self.assertEqual(pins['prompts']['presence'], watch.file_sha(prompts / 'presence.txt'))
        other = self.base / 'same_bytes_unqualified.json'
        other.write_bytes(kwargs['checker_config_path'].read_bytes())
        with self.assertRaisesRegex(ValueError, 'configured_reader_input_not_qualified'):
            watch.load_frozen(protocol, milestone, **{**kwargs, 'checker_config_path': other})
        bad = {'source_sha256': watch.file_sha(watch.reader.__file__),
               'pins': [{'path': '../outside', 'sha256': 'unreachable'}]}
        milestone.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, 'snapshot_path_not_canonical'):
            watch.load_frozen(protocol, milestone, **kwargs)

    def test_configuration_requires_explicit_paths_roster_and_worker_cap(self):
        config = {'schema': 'causes.reader_watch_configuration.v1', 'models': ['first', 'second'], 'workers': 4}
        for key in ('protocol', 'qualification', 'pin_base', 'reader_config', 'prompts',
                    'accepted', 'output', 'pause_file', 'env_file'):
            config[key] = str(self.base / key)
        path = self.base / 'watch_configuration.json'
        path.write_text(json.dumps(config))
        self.assertFalse(Path(config['env_file']).exists())
        self.assertEqual(watch.load_configuration(path)['models'], ('first', 'second'))
        for changed, reason in [({'models': ['first', 'first']}, 'explicit_distinct_model_roster'),
                                ({'models': ['../outside']}, 'explicit_distinct_model_roster'),
                                ({'accepted': 'relative/index'}, 'explicit_absolute_configuration_path'),
                                ({'workers': 5}, 'reader_context_worker_limit')]:
            path.write_text(json.dumps({**config, **changed}))
            with self.assertRaisesRegex(ValueError, reason):
                watch.load_configuration(path)


if __name__ == '__main__':
    unittest.main()
