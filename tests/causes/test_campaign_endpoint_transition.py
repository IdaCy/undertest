import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from causes import campaign_endpoint_transition as transition
from causes import campaign_handoff as h


def fixture(base, job=None, config=None, root='runs/successor/mi27'):
    base = Path(base)
    serial = 0
    def save(value):
        nonlocal serial
        serial += 1
        return h.save_new(base, 'transition_' + str(serial) + '.json', value)
    job = copy.deepcopy(job or {'spec': {'task_id': 'synthetic'}, 'variant': {'id': 'baseline'},
                                'allocation': {'run_id': 'fixture', 'continue_to_end': True}})
    original = dict(config or {}, model='synthetic', endpoint='http://127.0.0.1:18081/v1/chat/completions')
    current = dict(original, endpoint='http://127.0.0.1:18083/v1/chat/completions')
    provider = save({'host': 'synthetic-host', 'port': 18083, 'model': 'synthetic',
                     'process': {'pid': 123, 'process_starttime_ticks': '456', 'argv': ['synthetic-provider']}})
    backend = save({'backend': 'synthetic-backend', 'numerical_equivalence_claimed': False})
    current_pin, original_pin = save(current), save(original)
    qualification = {'schema': 'causes.storage_endpoint_qualification.v1', 'accepted': True,
                     'host': 'synthetic-host', 'provider': provider, 'backend_label': 'synthetic-backend',
                     'backend_provenance': backend, 'config': current_pin, 'endpoint': current['endpoint'],
                     'model': current['model'], 'qualified_max_full': 1,
                     'evidence': [save({'synthetic_finite_qualification': True})]}
    q = save(qualification)
    route = lambda value: 'model:' + hashlib.sha256(h.encoded([value['model'], value['endpoint']])).hexdigest()
    value = {'schema': 'causes.storage_endpoint_transition.v1', 'original_config': original_pin,
             'successor_config': current_pin, 'jobs': save([job]), 'run_ids': [job['allocation']['run_id']],
             'campaign_root': str(base), 'output_root': str(base / root), 'old_route_requalified': False,
             'old_route': route(original), 'new_route': route(current), 'host': 'synthetic-host',
             'provider': provider, 'backend_label': 'synthetic-backend', 'backend_provenance': backend,
             'qualification': {'evidence': q, 'review': save({
                 'accepted_storage_endpoint_qualification': True, 'qualification': q})}}
    def accepted(value):
        ref = save(value)
        return {'evidence': ref, 'review': save({'accepted_storage_endpoint_transition': True, 'transition': ref})}
    return {'pair': accepted(value), 'value': value, 'original': original, 'current': current,
            'job': job, 'root': root, 'save': save, 'accepted': accepted}


class EndpointTransitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.f = fixture(self.directory.name, config={'sampling': {'temperature': 1}, 'request_timeout_seconds': 600})

    def test_exact_mapping_preserves_frozen_digest_and_separate_backend(self):
        f = self.f
        actual, provenance = transition.snapshot_config(f['pair'], root=f['root'], job=f['job'],
            original_config=f['original'], original_file_sha256=f['value']['original_config']['sha256'])
        self.assertEqual(actual, f['current'])
        self.assertEqual(provenance['original_config'], f['value']['original_config'])
        self.assertEqual(provenance['captured_config'], f['value']['successor_config'])
        self.assertEqual(provenance['backend_label'], 'synthetic-backend')
        self.assertEqual(transition.validate(f['pair'])['snapshot_root'], f['root'])

    def test_endpoint_exception_does_not_allow_numeric_type_change(self):
        f = self.f
        candidate = copy.deepcopy(f['current']);candidate['sampling']['temperature'] = 1.0
        value = dict(f['value'], successor_config=f['save'](candidate))
        with self.assertRaisesRegex(ValueError, 'endpoint_only_config'):
            transition.validate(f['accepted'](value))

    def test_mapping_rejects_other_root_job_and_original_file(self):
        f = self.f
        kwargs = dict(root=f['root'], job=f['job'], original_config=f['original'],
                      original_file_sha256=f['value']['original_config']['sha256'])
        other_job = copy.deepcopy(f['job']);other_job['allocation']['run_id'] = 'another'
        for change in ({'root': 'runs/original/mi27'}, {'job': other_job},
                       {'original_file_sha256': '0' * 64}, {'original_config': f['current']}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'frozen_scope_mismatch'):
                transition.snapshot_config(f['pair'], **dict(kwargs, **change))

    def test_rejected_review_and_changed_backend_bytes_fail_closed(self):
        f = self.f
        bad = dict(f['pair'], review=f['save']({'accepted_storage_endpoint_transition': False,
                                               'transition': f['pair']['evidence']}))
        with self.assertRaisesRegex(ValueError, 'unaccepted_storage_input'):
            transition.validate(bad)
        Path(f['value']['backend_provenance']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            transition.validate(f['pair'])


if __name__ == '__main__':
    unittest.main()
