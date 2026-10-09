import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_observation as o
from causes import campaign_observation_entrypoint as entry


def fixture(root):
    def save(name, value):
        path = root / name
        path.write_text(json.dumps(value))
        return o.pin(path)
    overlay = root / 'overlay.py'
    overlay.write_text("from pathlib import Path\nimport json\ndef validate_authorization(path): return json.loads(Path(path).read_bytes())\ndef pinned(ref): return Path(ref['path']).read_bytes()\n")
    roots = save('legacy-roots.json', {'roots':['runs/legacy']})
    manifest = save('manifest.json', {'outputs':{'current':str(root/'runs/legacy')}})
    auth = save('overlay.json', {'overlay_module':o.pin(overlay),'manifest':manifest})
    launch = save('launch.json', {'bundle':'bundle','source_identities':{'runner.py':'a'*64},
        'processes':[{'pid':1,'model':'a','argv':['python3','runner']}]})
    policy = {'schema':'causes.campaign_monitor_policy.v1','required':True,
        'remote_base':str(root),'controller_host':'provider','transfer_host':'tasks',
        'sync_roots':['runs/current'],'phases':[{'phase':'current','mode':'finite',
            'output_root':str(root/'runs/current'),'workers':8}],
        'host_transfer':{'target_output_roots':[str(root/'runs/current')]},
        'legacy':{'owners':[{'pid':1,'model':'a'},{'pid':2,'model':'b'}],
            'sync_roots':roots,'sync_roots_value':{'roots':['runs/legacy']},
            'local_pins':[auth],'identity_receipt':manifest},
        'tunnels':[{},{}],'provider_port':8000,'relay_port':8001,
        'max_model_len':100,'minimum_gpu_free_mib':10,'max_observation_age_seconds':180}
    policy_pin = save('policy.json',policy)
    config = {'schema':'causes.campaign_observation_config.v1',
        'sources':{Path(m.__file__).name:o.pin(m.__file__) for m in (o,o.monitor,o.snapshot)},
        'monitor':{'policy':policy_pin,'authorization_path':str(root/'authorization.json')},
        'legacy':{'host':'tasks','remote_base':str(root),'sync_roots':roots,
            'launch':launch,'overlay_authorization':auth,
            'observation':{'disk_paths':[str(root)],'docker_filter':'label=campaign=example',
                'endpoints':[[8000,'example']],'profile_name':'example',
                'root_free_floor_bytes':80*1024**3,'available_memory_floor_kib':16*1024**2}},
        'snapshot':{'host':'provider','remote_base':str(root),'roots':['runs/current'],
            'destination':str(root/'snapshots'),'limit':2,'free_space_floor_bytes':0},
        'status_directory':str(root),'transport_directory':str(root/'transport')}
    return config


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture(self.root)

    def test_request_preserves_overlay_and_exact_legacy_inventory(self):
        o.validate_config(self.config)
        value = o.legacy_request(self.config)
        self.assertEqual(value['roots'], ['runs/legacy'])
        self.assertEqual(value['identity']['overlay_authorization'],
                         o.checked_json(self.config['legacy']['overlay_authorization']))
        self.assertEqual(value['identity']['sources'], {'runner.py':'a'*64})
        self.assertEqual(value['observation'], self.config['legacy']['observation'])

    def test_source_input_and_snapshot_scope_drift_refused_before_transport(self):
        for change in (lambda c:c['sources']['campaign_monitor.py'].update(sha256='0'*64),
                       lambda c:c['snapshot']['roots'].append('runs/unregistered'),
                       lambda c:c['legacy']['observation'].update(root_free_floor_bytes=0),
                       lambda c:c['legacy'].update(host='other')):
            config = copy.deepcopy(self.config); change(config)
            with patch.object(o.subprocess,'run') as run:
                with self.assertRaises(ValueError):o.validate_config(config)
                run.assert_not_called()
        Path(self.config['legacy']['launch']['path']).write_text('{}')
        with self.assertRaises(ValueError):o.validate_config(self.config)

    def test_status_observes_legacy_once_and_uses_public_schema(self):
        legacy = {'host':'tasks','at':'2026-01-01T00:00:00+00:00','alarms':{'legacy_alarm':False}}
        def companion(config, callback):
            self.assertEqual(callback()['host'],'tasks')
            return {'required':True,'valid':True,'alarms':{'route_history_blocked':False}}
        with patch.object(o.subprocess,'run',return_value=SimpleNamespace(returncode=0,
                    stdout=json.dumps(legacy).encode(),stderr=b'')) as run, \
                patch.object(o.monitor,'observe',side_effect=companion):
            ref,value = o.status(self.config)
        self.assertEqual(run.call_count,1)
        self.assertIn('companion',value)
        self.assertNotIn('jp_companion',value)
        self.assertFalse(value['alarms']['companion_missing_or_invalid'])
        self.assertEqual(o.checked_json(ref),value)
        self.assertEqual(len(list((self.root/'transport').glob('*/EXECUTION.json'))),1)

    def test_transport_failure_retains_receipt_without_status(self):
        with patch.object(o.subprocess,'run',return_value=SimpleNamespace(returncode=255,
                    stdout=b'',stderr=b'transport unavailable')):
            with self.assertRaisesRegex(ValueError,'legacy_observation_failed'):o.status(self.config)
        self.assertFalse(list(self.root.glob('*_job_status.json')))
        executions = list((self.root/'transport').glob('*/EXECUTION.json'))
        self.assertEqual(len(executions),1)
        self.assertEqual(json.loads(executions[0].read_bytes())['returncode'],255)

    def test_sync_reobserves_and_keeps_static_authorization_across_history_alarm(self):
        calls = []
        def observe(config, callback):
            calls.append(callback())
            return {'required':True,'valid':True,'alarms':{'companion_missing_or_invalid':False,
                'route_history_blocked':True},'authorization':{'stable':'auth'},
                'policy':config.policy_pin,'registration':{'stable':'reg'},'review':{'stable':'review'}}
        def synchronize(config, authorize):
            first = authorize(config); second = authorize(config)
            self.assertEqual(first,second)
            self.assertEqual(first['roots'],['runs/current'])
            return {'verified_runs':0}
        with patch.object(o.LegacyObserver,'__call__',side_effect=[{'cut':1},{'cut':2}]), \
                patch.object(o.monitor,'observe',side_effect=observe), \
                patch.object(o.snapshot,'synchronize_completed',side_effect=synchronize):
            self.assertEqual(o.synchronize(self.config),{'verified_runs':0})
        self.assertEqual(calls,[{'cut':1},{'cut':2}])

    def test_configuration_json_rejects_ambiguous_identity(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):o.strict_json(raw)

    def test_adjacent_entrypoint_rejects_wrong_action_or_source_location(self):
        directory = self.root/'code'; package = directory/'causes'
        package.mkdir(parents=True)
        sources = {}
        for name, ref in self.config['sources'].items():
            target = package/name; target.write_bytes(Path(ref['path']).read_bytes())
            sources[name] = o.pin(target)
        (package/'__init__.py').write_bytes(b'')
        sources['__init__.py'] = o.pin(package/'__init__.py')
        self.config['sources'] = {k:v for k,v in sources.items() if k != '__init__.py'}
        observer = o.save(self.root/'observation.json',self.config)
        source = self.root/'read_status.py'; source.write_bytes(Path(entry.__file__).read_bytes())
        config = {'schema':'causes.campaign_observation_entrypoint.v1','action':'status',
            'entrypoint_sha256':o.pin(source)['sha256'],'code_directory':str(directory),
            'sources':sources,'observation_config':observer}
        path = self.root/'read_status.config.json'
        path.write_text(json.dumps(config))
        self.assertEqual(entry.prepare(path,source),config)
        for change in (lambda c:c.update(action='launch'),
                       lambda c:c['sources']['campaign_monitor.py'].update(path=str(self.root/'other.py')),
                       lambda c:c.update(entrypoint_sha256='0'*64)):
            changed = copy.deepcopy(config); change(changed); path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):entry.prepare(path,source)


if __name__ == '__main__':
    unittest.main()
