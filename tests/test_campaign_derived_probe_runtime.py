import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from causes import campaign_supplement as core
from causes import campaign_derived_probe_runtime as runtime


INSERTION = ('                    if any(future.done() for future in active):\n'
             '                        break  # Harvest outcomes before preparing another unclaimed request.\n')


class Fixture:
    def __init__(self, root, changed=False):
        self.root = root
        old = Path(core.__file__).read_bytes()
        new = old.replace(b'                    observation = admission_check()\n',
                          b'                    observation = admission_check()\n' + INSERTION.encode()) if changed else old
        self.scientific = self.module('scientific', old)
        self.operational = self.module('operational', new)
        self.old = core.pin(self.scientific.__file__)
        self.new = core.pin(self.operational.__file__)
        self.review = None
        if changed:
            self.review = self.save('review', dict(accepted_admission_harvest_optimization_source=True,
                accepted_supplement_admission_harvest_source=True, source=self.new, findings=[],
                complete_inverse=dict(parent_bytes=len(old), parent_sha256=hashlib.sha256(old).hexdigest(),
                    added_bytes=158, forward_exact=True, reverse_exact=True, all_other_bytes_literal=True,
                    changed_top_level_helpers=['run_queue'])))
        self.protocol = dict(sources=dict(campaign_supplement=self.old),
                             config=dict(endpoint='http://synthetic', seed=17, parser='fixed'))
        self.calls = []
        self.prepare_error = None
        self.row = dict(synthetic=True, missing=False)
        self.job = dict(measurement_id='derived/model/row/early/evaluation_presence', seed=17)
        self.adapter = types.SimpleNamespace(supplement=self.scientific, runner=object(),
            _protocol=lambda p:(copy.deepcopy(p['config']), dict(identity='frozen')),
            prepare_probe=self.prepare_probe)

    def module(self, name, raw):
        path = self.root / (name + '.py')
        path.write_bytes(raw)
        module = types.ModuleType(name)
        module.__file__ = str(path)
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        return module

    def save(self, name, value):
        path = self.root / (name + '.json')
        core.save(path, value)
        return core.pin(path)

    def prepare_probe(self, job, row, protocol, config):
        self.calls.append(('prepare', copy.deepcopy(job), copy.deepcopy(row)))
        if self.prepare_error:
            raise self.prepare_error
        return [dict(role='user', content='synthetic context')], dict(id='evaluation_presence')

    def guard(self, job):
        self.calls.append(('source', copy.deepcopy(job)))

    def build(self, **kwargs):
        return runtime.FrozenProbeAdapter(self.adapter, self.operational, self.protocol,
            {self.job['measurement_id']:self.row}, scientific_source=self.old,
            operational_source=self.new, operational_review=self.review,
            source_check=self.guard, **kwargs)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def test_literal_native_source_with_distinct_exception_classes(self):
        f = Fixture(self.root)
        adapter = f.build()
        self.assertIsNot(f.scientific.IntegrityError, f.operational.IntegrityError)
        self.assertEqual(adapter.prepare(f.job)[1]['id'], 'evaluation_presence')
        self.assertEqual([x[0] for x in f.calls], ['source', 'prepare'])

    def test_reviewed_queue_change_preserves_frozen_protocol_and_rows(self):
        f = Fixture(self.root, changed=True)
        frozen = copy.deepcopy(f.protocol)
        adapter = f.build()
        f.row['synthetic'] = False
        adapter.prepare(f.job)
        self.assertEqual(adapter.protocol, frozen)
        self.assertTrue(f.calls[-1][2]['synthetic'])
        self.assertEqual(adapter.protocol['sources']['campaign_supplement'], f.old)
        self.assertNotEqual(f.old['sha256'], f.new['sha256'])

    def test_source_pin_and_loaded_file_mutations_refuse(self):
        f = Fixture(self.root)
        wrong = dict(f.old, sha256='0'*64)
        with self.assertRaises((core.IntegrityError, f.operational.IntegrityError)):
            runtime.compatible_queue(f.scientific, f.operational, wrong, f.new)
        f.scientific.__file__ = f.new['path']
        Path(f.new['path']).write_bytes(b'# changed\n')
        with self.assertRaises((core.IntegrityError, f.operational.IntegrityError)):
            f.build()

    def test_migration_requires_exact_review_and_unchanged_request_helpers(self):
        f = Fixture(self.root, changed=True)
        with self.assertRaises(f.operational.IntegrityError):
            runtime.compatible_queue(f.scientific, f.operational, f.old, f.new)
        peer = json.loads(core.pinned(f.review))
        peer['complete_inverse']['forward_exact'] = False
        f.review = f.save('bad-review', peer)
        with self.assertRaises(f.operational.IntegrityError):f.build()
        peer['complete_inverse']['forward_exact'] = True
        changed = Path(f.operational.__file__).read_bytes().replace(b'80 * 1024**3', b'81 * 1024**3')
        Path(f.operational.__file__).write_bytes(changed)
        f.new = core.pin(f.operational.__file__)
        peer['source'] = f.new
        f.review = f.save('helper-review', peer)
        with self.assertRaisesRegex(f.operational.IntegrityError, 'request_helpers_changed'):f.build()

    def test_endpoint_only_change_retains_scientific_configuration(self):
        f = Fixture(self.root)
        changed = copy.deepcopy(f.protocol)
        changed['config']['endpoint'] = 'http://another-synthetic'
        adapter = f.build(execution_protocol=changed)
        self.assertEqual(adapter.config['seed'], 17)
        self.assertEqual(adapter.config['endpoint'], changed['config']['endpoint'])
        changed['config']['seed'] = 18
        with self.assertRaisesRegex(f.operational.IntegrityError, 'semantic_protocol_changed'):
            f.build(execution_protocol=changed)

    def test_unknown_job_and_missing_source_callback_refuse(self):
        f = Fixture(self.root)
        adapter = f.build()
        with self.assertRaisesRegex(f.operational.IntegrityError, 'unknown_job'):
            adapter.prepare(dict(measurement_id='unknown'))
        with self.assertRaisesRegex(f.operational.IntegrityError, 'source_callback_required'):
            runtime.FrozenProbeAdapter(f.adapter, f.operational, f.protocol, {},
                scientific_source=f.old, operational_source=f.new, source_check=None)

    def test_only_integrity_error_is_translated_without_retry(self):
        f = Fixture(self.root)
        adapter = f.build()
        f.prepare_error = f.scientific.IntegrityError('changed_request')
        with self.assertRaisesRegex(f.operational.IntegrityError, 'changed_request') as caught:
            adapter.prepare(f.job)
        self.assertIs(caught.exception.__cause__, f.prepare_error)
        for error in (f.scientific.AdmissionHold('freshness'), RuntimeError('request_failure')):
            f.prepare_error = error
            with self.assertRaises(type(error)) as caught:adapter.prepare(f.job)
            self.assertIs(caught.exception, error)
        self.assertEqual(sum(x[0]=='prepare' for x in f.calls), 3)

    def test_worker_uses_native_function_and_checks_both_boundaries(self):
        f = Fixture(self.root)
        adapter = f.build()
        def native(job, destination, prepare, config, runner):
            self.assertEqual(job, f.job)
            self.assertEqual(destination, self.root / 'unused')
            self.assertIs(runner, f.adapter.runner)
            prepare(job)
            f.calls.append(('native_done',))
            return dict(technical_status='complete')
        with mock.patch.object(f.operational, 'probe_measurement', native):
            self.assertEqual(adapter.worker(f.job, self.root/'unused'), dict(technical_status='complete'))
        self.assertEqual([x[0] for x in f.calls], ['source','source','prepare','native_done','source'])

    def test_post_worker_source_change_and_native_failure_are_terminal(self):
        f = Fixture(self.root)
        adapter = f.build()
        def changed(job, destination, prepare, config, runner):
            adapter.source_check = lambda job: (_ for _ in ()).throw(f.operational.IntegrityError('exit_changed'))
            return dict(technical_status='complete')
        with mock.patch.object(f.operational, 'probe_measurement', changed), \
                self.assertRaisesRegex(f.operational.IntegrityError, 'exit_changed'):
            adapter.worker(f.job, self.root/'unused')
        adapter.source_check = f.guard
        error = RuntimeError('native_failure')
        with mock.patch.object(f.operational, 'probe_measurement', side_effect=error) as native, \
                self.assertRaises(RuntimeError) as caught:
            adapter.worker(f.job, self.root/'unused')
        self.assertIs(caught.exception, error)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(f.calls[-1][0], 'source')


if __name__ == '__main__':
    unittest.main()
