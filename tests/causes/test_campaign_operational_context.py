import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_operational_context as context


class OperationalContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.campaign = Path(self.tmp.name)
        self.hosts = self.campaign / 'hosts'
        self.hosts.mkdir()
        self.watch = self.hosts / 'watch'
        self.watch.mkdir()
        self.task = self.campaign / 'tasks/binding'
        self.task.mkdir(parents=True)
        self.cut = self.campaign / 'heartbeat/OPERATIONAL_TICK_fixture'
        self.cut.mkdir(parents=True)
        self.save(self.cut / 'OBSERVATION.json', {'fixture': 'fixed-reader seal'})
        script = self.hosts / 'read_status_successors.py'
        script.write_text('pass\n')
        config = self.save(script.with_suffix('.config.json'), {
            'action': 'status', 'entrypoint_sha256': context.pin(script)['sha256']})
        registry = self.save(self.task / 'REGISTRY.json', {
            'schema': 'causes.output_root_registry.v1',
            'sync_roots': ['runs/original', 'runs/new']})
        phase = self.save(self.task / 'PHASE.json', {'phase': 'successors_running'})
        self.binding = {'schema': 'causes.operational_context_binding.v1',
            'source': context.pin(Path(context.__file__).resolve()), 'campaign_root': str(self.campaign),
            'registry': registry, 'physical_group_roots': ['runs/original', 'runs/new'],
            'status_entries': {script.name: {'source': context.pin(script), 'config': config}},
            'phase_inputs': {'registration': phase}, 'previous_qualified_jp_route_failures': 3,
            'companion_host': 'companion-node'}
        self.preparation = self.task / 'BINDING.json'
        self.review = self.task / 'REVIEW.json'
        self.bind()

    def save(self, path, value):
        path.write_text(json.dumps(value) + '\n')
        return context.pin(path)

    def bind(self):
        ref = self.save(self.preparation, self.binding)
        self.save(self.review, {'accepted_operational_context_binding': True, 'binding': ref})

    def cycle(self, name, ended, script='read_status_successors.py', status=None, failed=False):
        directory = self.watch / name
        directory.mkdir()
        self.save(directory / 'RESULT.json', {'ended_at': ended,
            'checks': [{'script': script, 'returncode': 1 if failed else 0}]})
        if status is not None:
            status_path = self.hosts / (name + '_status.json')
            serving_path = self.hosts / (name + '_serving.json')
            self.save(status_path, status)
            self.save(serving_path, {'fixture': 'serving'})
            (directory / (script + '.stdout')).write_text(str(status_path) + '\n')
            (directory / 'sample_serving_activity.py.stdout').write_text(str(serving_path) + '\n')
        return directory

    def test_latest_failed_cycle_wins_and_future_ended_is_excluded(self):
        self.cycle('older', '2026-01-01T00:00:00+00:00')
        self.cycle('failed', '2026-01-01T00:01:00+00:00', failed=True)
        self.cycle('future', '2026-01-01T00:03:00+00:00')
        cutoff = dt.datetime.fromisoformat('2026-01-01T00:01:00+00:00')
        path, raw, value, count = context.select_latest(self.watch, cutoff)
        self.assertEqual(path.parent.name, 'failed')
        self.assertEqual(count, 2)
        self.assertEqual(value['checks'][0]['returncode'], 1)

    def test_retains_invalid_companion_and_registered_new_root_without_qualification(self):
        status = {'at': '2026-01-01T00:00:00+00:00',
            'companion': {'valid': False, 'error_type': 'retained_fault'},
            'alarms': {'root_free_below_80GiB': True}, 'disks': [{'free': 10}],
            'source_identity': {'storage_phase': 'successors_running'},
            'groups': [{'root': 'runs/original'}, {'root': 'runs/new'}, {'root': 'unregistered'}]}
        self.cycle('current', status['at'], status=status)
        result = json.loads(Path(context.retain(self.cut, self.preparation, self.review)['path']).read_bytes())
        self.assertEqual(result['groups_physical_root_only'], status['groups'][:2])
        self.assertTrue(result['alarms']['root_free_below_80GiB'])
        self.assertFalse(result['jp_current_context_available'])
        self.assertIsNone(result['jp'])
        self.assertFalse(result['qualified_JP_context_accepted'])
        self.assertEqual(result['status_script'], 'read_status_successors.py')
        self.assertEqual(result['phase_inputs'], self.binding['phase_inputs'])

    def test_unknown_latest_status_binding_refuses_without_older_fallback(self):
        self.cycle('older', '2026-01-01T00:00:00+00:00')
        self.cycle('newer', '2026-01-01T00:01:00+00:00', script='old_or_unknown.py', failed=True)
        with self.assertRaisesRegex(AssertionError, 'selected_cycle_status_binding_unavailable'):
            context.retain(self.cut, self.preparation, self.review)
        receipt = json.loads((self.cut / 'ORDINARY_SELECTION.v5.json').read_bytes())
        self.assertEqual(Path(receipt['selected_result']['path']).parent.name, 'newer')
        self.assertFalse((self.cut / 'RETAINED_ORDINARY_CONTEXT.v5.json').exists())

    def test_binding_review_source_registry_and_phase_drift_refuse(self):
        original = copy.deepcopy(self.binding)
        for kind in ('source', 'registry', 'phase', 'group', 'companion_host'):
            self.binding = copy.deepcopy(original)
            if kind == 'source':
                self.binding['source']['sha256'] = 'wrong'
            elif kind == 'registry':
                self.binding['registry']['sha256'] = 'wrong'
            elif kind == 'phase':
                self.binding['phase_inputs'] = {}
            elif kind == 'companion_host':
                self.binding['companion_host'] = ''
            else:
                self.binding['physical_group_roots'].append('unregistered')
            self.bind()
            with self.subTest(kind=kind), self.assertRaises(AssertionError):
                context.context_binding(self.preparation, self.review)
        self.binding = original
        self.bind()
        self.save(self.review, {'accepted_operational_context_binding': False,
                              'binding': context.pin(self.preparation)})
        with self.assertRaises(AssertionError):
            context.context_binding(self.preparation, self.review)

    def test_changed_status_source_or_adjacent_config_refuses(self):
        entry = next(iter(self.binding['status_entries'].values()))
        path = Path(entry['source']['path'])
        raw = path.read_bytes()
        path.write_bytes(raw + b'pass\n')
        with self.assertRaisesRegex(AssertionError, 'context_status_source_changed'):
            context.context_binding(self.preparation, self.review)
        path.write_bytes(raw)
        Path(entry['config']['path']).write_text('{}')
        with self.assertRaisesRegex(AssertionError, 'context_input_pin_changed'):
            context.context_binding(self.preparation, self.review)

    def test_valid_jp_projection_stays_raw_and_invalid_nested_fields_become_unavailable(self):
        candidate = {'at': '2026-01-01T00:00:00+00:00', 'controller': {}, 'provider': {}, 'gpu': {},
            'lock_owner': [], 'metrics': [], 'journal': {'event_counts': {'route_failure': 4}},
            'valid': True, 'prior_local_owners_closed': True}
        valid = {'valid': True, 'hosts': [{'host': 'companion-node', 'observation': candidate}]}
        result = context.companion_projection(valid, 3, self.binding['companion_host'])
        self.assertTrue(result['jp_current_context_available'])
        self.assertEqual(result['jp_reporting']['current_raw_aggregate_route_failures'], 4)
        self.assertFalse(result['qualified_JP_context_accepted'])
        self.assertFalse(context.companion_projection(valid, 3, 'another-node')['jp_current_context_available'])
        for key, value in [('valid', False), ('prior_local_owners_closed', 1), ('controller', None),
                           ('journal', {'event_counts': {'route_failure': True}})]:
            changed = copy.deepcopy(valid)
            changed['hosts'][0]['observation'][key] = value
            with self.subTest(key=key):
                result = context.companion_projection(changed, 3, self.binding['companion_host'])
                self.assertFalse(result['jp_current_context_available'])
                self.assertIsNone(result['jp_reporting']['current_raw_aggregate_route_failures'])


if __name__ == '__main__':
    unittest.main()
