import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_census
from causes.census_preload import CensusPreload, reference
from causes.pinned_preload import PinnedPreload


class CensusPreloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base/'census.py'
        self.source.write_bytes(Path(campaign_census.__file__).read_bytes())
        spec = importlib.util.spec_from_file_location('saved_test_census', self.source)
        self.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.native)
        self.hosts = []
        for name in ('one', 'two'):
            base = self.base/name
            (base/'runs/finite/model/.dispatcher/blocks').mkdir(parents=True)
            self.hosts.append(dict(host=name, base=str(base), roots=['runs/finite/model'],
                top_jobs=[], control_directories=[], limits=dict(max_file_bytes=10000,
                max_total_bytes=100000, max_files=20, max_directory_entries=20, max_allocations=30)))
        self.frozen = self.base/'frozen.json'
        self.frozen.write_bytes(self.native.encoded([dict(run_id='future', seed=7,
            block_seed=2, task_seed=None, model_seed=3)]))
        self.config_value = dict(schema=self.native.CONFIG_SCHEMA,
            collector_sha256=reference(self.source)['sha256'], hosts=self.hosts,
            max_capture_bytes=1000000, max_frozen_bytes=100000,
            frozen_inputs=[dict(pin=reference(self.frozen), selector=[], kind='allocations', expected_count=1)])
        self.config = self.base/'config.json'
        self.config.write_bytes(self.native.encoded(self.config_value))
        self.capture_map = self.base/'captures.json'
        captures = {}
        for host in self.hosts:
            value = self.native.collect_host(host)
            value['collector_source'] = reference(self.source)
            p = self.base/(host['host']+'.json')
            p.write_bytes(self.native.encoded(value))
            captures[host['host']] = reference(p)
        self.capture_map.write_bytes(self.native.encoded(captures))

    def actor(self):
        return CensusPreload(reference(self.source), reference(self.config))

    def args(self, output='candidate.json'):
        return ['compose', '--config', str(self.config), '--captures',
                str(self.capture_map), '--output', str(self.base/output)]

    def execute(self, actor):
        with contextlib.redirect_stdout(io.StringIO()):
            actor.execute(self.args())

    def test_complete_native_output_and_true_execution_provenance(self):
        actor = self.actor()
        actor.prepare()
        with contextlib.redirect_stdout(io.StringIO()):
            self.native.main(self.args('original.json'))
        self.execute(actor)
        self.assertEqual((self.base/'original.json').read_bytes(), (self.base/'candidate.json').read_bytes())
        evidence = actor.evidence()
        self.assertFalse(evidence['preparation']['capture_inputs_read'])
        self.assertTrue(evidence['composition']['immutable_finish_verified'])
        self.assertFalse(evidence['composition']['native_cli_invoked'])
        self.assertEqual(evidence['composition']['native_source'], reference(self.source))
        with self.assertRaisesRegex(ValueError, 'preload_not_prepared'):
            self.execute(actor)

    def test_capture_is_read_at_consumption_and_must_match_configured_hosts(self):
        actor = self.actor(); actor.prepare()
        self.capture_map.write_bytes(self.native.encoded({}))
        with self.assertRaisesRegex(ValueError, 'exact_census_inputs_required'):
            self.execute(actor)
        self.assertFalse((self.base/'candidate.json').exists())

    def test_frozen_source_and_configuration_mutation_before_consumption(self):
        for name in ('frozen', 'source', 'config'):
            with self.subTest(name=name):
                p = getattr(self, name); original = p.read_bytes()
                actor = self.actor(); actor.prepare()
                p.write_bytes(original+b' ')
                with self.assertRaisesRegex(ValueError, 'preload_file_identity_changed'):
                    self.execute(actor)
                p.write_bytes(original)
        self.assertFalse((self.base/'candidate.json').exists())

    def test_same_bytes_replacement_is_rejected(self):
        actor = self.actor(); actor.prepare()
        self.frozen.rename(self.base/'original-frozen.json')
        self.frozen.write_bytes((self.base/'original-frozen.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'preload_file_identity_changed'):
            self.execute(actor)

    def test_finish_rechecks_bytes_and_does_not_publish_success_evidence(self):
        actor = self.actor(); actor.prepare()
        finish = PinnedPreload.finish
        def mutate_then_finish(preload):
            self.frozen.write_bytes(self.frozen.read_bytes()+b' ')
            return finish(preload)
        with patch.object(PinnedPreload, 'finish', mutate_then_finish):
            with self.assertRaisesRegex(ValueError, 'preload_file_identity_changed'):
                self.execute(actor)
        self.assertNotIn('composition', actor.evidence())
        self.assertTrue((self.base/'candidate.json').exists())

    def test_configuration_cannot_be_switched_at_consumption(self):
        actor = self.actor(); actor.prepare()
        other = self.base/'other.json'; other.write_bytes(self.config.read_bytes())
        args = self.args(); args[2] = str(other)
        with self.assertRaisesRegex(ValueError, 'preloaded_compose_arguments_required'):
            actor.execute(args)

    def test_declared_frozen_bound_and_count_remain_enforced(self):
        for field, value, message in [('max_frozen_bytes', 1, 'census_file_type_or_size'),
                                       ('expected_count', 2, 'frozen_reservation_count')]:
            with self.subTest(field=field):
                config = copy.deepcopy(self.config_value)
                if field == 'expected_count': config['frozen_inputs'][0][field] = value
                else: config[field] = value
                self.config.write_bytes(self.native.encoded(config))
                with self.assertRaisesRegex(ValueError, message): self.actor().prepare()

    def test_native_exclusive_output_is_preserved(self):
        actor = self.actor(); actor.prepare()
        output = self.base/'candidate.json'; output.write_bytes(b'preserve me')
        with self.assertRaises(FileExistsError): self.execute(actor)
        self.assertEqual(output.read_bytes(), b'preserve me')


if __name__ == '__main__':
    unittest.main()
