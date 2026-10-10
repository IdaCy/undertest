import copy
import datetime as dt
import hashlib
import json
import unittest
from unittest.mock import patch

from causes import campaign_monitor as m
from tests.causes.test_campaign_monitor import fixture


def storage_fixture(phase='texas_running_mi_retired'):
    policy, _, _, _ = fixture()
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat()
    objects = {}
    def ref(name, value):
        raw = json.dumps(value, sort_keys=True).encode()
        result = {'path': '/fixture/' + name, 'bytes': len(raw),
                  'sha256': hashlib.sha256(raw).hexdigest()}
        objects[result['path']] = value
        return result
    source = b'qualified observer fixture'
    source_ref = {'path': '/fixture/observer.py', 'bytes': len(source),
                  'sha256': hashlib.sha256(source).hexdigest()}
    manifest = ref('manifest', {'identity': 'bound'})
    roles = m.STORAGE_PHASE_ROLES[phase]
    owners, observations, final, models = {}, {}, {}, {}
    for number, (role, state) in enumerate(roles.items(), 10):
        process = {'pid': number, 'process_starttime_ticks': str(number * 100),
                   'argv': ['python', 'owned_' + role]}
        root = '/fixture/runs/' + role
        queue = {'model': 'fixed', 'jobs_sha256': role}
        owner = {'process': process, 'output_root': root, 'observation_state': state,
                 'queue_identity': queue,
                 'queue_identity_pin': ref(role + '_queue', queue),
                 'controller_identity_pin': ref(role + '_controller', {'owned': role}),
                 'journal_prefix': ref(role + '_journal', {'prefix': role})}
        owners[role] = owner
        models[role] = 'mi27' if role.endswith('mi') else 'tex9'
        ownership = {'pid': number, 'absent': True} if state == 'retired' else copy.deepcopy(process)
        row = {'process': copy.deepcopy(process), 'output_root': root,
               'queue_identity': queue, 'live': state != 'retired',
               'lock_owner': None if state == 'retired' else number,
               'claims': 0, 'active_claims': 0, 'persisted_blocks': 0,
               'observed_from': stamp, 'observed_until': stamp,
               'process_before': copy.deepcopy(ownership), 'process_after': copy.deepcopy(ownership),
               'metadata_pins': {key: owner[key] for key in ('queue_identity_pin', 'controller_identity_pin')}}
        row['metadata_pins']['journal'] = owner['journal_prefix']
        if role.startswith('new_'):
            row.update(barrier_present=state == 'waiting',
                       guard={'healthy': True, 'alarms': {key: False for key in
                           ('owner_identity_invalid', 'reporting_lock_invalid', 'guard_record_invalid',
                            'guard_not_ready', 'storage_binding_invalid', 'volume_below_floor')}, 'at': stamp})
        observations[role] = row
        final[role] = ownership
    registration = {'schema': 'causes.storage_controller_phase.v1', 'phase': phase,
                    'owners': owners, 'identity_manifest': manifest}
    reg_ref = ref('registration', registration)
    review_ref = ref('review', {'accepted_storage_phase_registration': True, 'registration': reg_ref})
    adoption_ref = ref('adoption', {'root_accepted': True, 'registration': reg_ref,
                                   'independent_review': review_ref, 'manifest': manifest})
    transition = {'schema': 'causes.storage_legacy_monitor_policy.v1', 'phase': phase,
                  'registration': reg_ref, 'independent_review': review_ref,
                  'adoption': adoption_ref, 'observer_source': source_ref, 'role_models': models}
    policy['legacy']['storage_transition'] = transition
    policy['legacy']['identity_receipt'] = reg_ref
    policy['legacy']['owners'] = sorted(
        ({'pid': owners[role]['process']['pid'], 'model': models[role]}
         for role, state in roles.items() if state != 'retired'), key=lambda row: row['model'])
    evidence = {'schema': 'causes.storage_phase_observation.v1', 'observed_from': stamp,
                'observed_until': stamp, 'source_sha256': source_ref['sha256'],
                'decision': {'phase': phase, 'roles': copy.deepcopy(roles),
                             'processes_restarted': False, 'runtime_actions': False},
                'observations': observations, 'final_ownership': final, 'runtime_actions': False}
    status = {'at': stamp, 'alarms': {key: False for key in policy['legacy']['alarm_keys']},
              'source_identity': {'all_expected_identities_match': True, 'kernel_profile_name_loaded': True,
              'overlay': {'authorized': True, 'valid': True, 'receipt': reg_ref, 'phase': phase,
                          'manifest_sha256': manifest['sha256'], 'succession_observations': [evidence]},
              'controllers': [{**row, 'live': True, 'argv_matches': True, 'starttime_matches': True}
                              for row in policy['legacy']['owners']],
              'files': [{**row, 'matches': True, 'actual_sha256': row['expected_sha256']}
                        for row in policy['legacy']['files']]}}
    return policy, status, objects, source, now


class StorageMonitorTests(unittest.TestCase):
    def validate(self, values):
        policy, status, objects, source, now = values
        with patch.object(m, 'checked_json', side_effect=lambda ref: copy.deepcopy(objects[ref['path']])), \
                patch.object(m, 'checked', return_value=source):
            m.validate_policy(policy)
            m.validate_legacy(status, policy, now)

    def test_exact_registered_mixed_phases_and_single_live_owner_are_accepted(self):
        for phase in m.STORAGE_PHASE_ROLES:
            with self.subTest(phase=phase):
                values = storage_fixture(phase)
                self.validate(values)
                expected = 1 if phase in ('texas_waiting_mi_retired', 'texas_running_mi_retired') else 2
                self.assertEqual(len(values[1]['source_identity']['controllers']), expected)
                self.assertEqual([row['model'] for row in values[1]['source_identity']['controllers']],
                                 ['tex9'] if expected == 1 else ['mi27', 'tex9'])

    def test_one_owner_without_explicit_storage_policy_is_rejected(self):
        values = storage_fixture()
        values[0]['legacy'].pop('storage_transition')
        with self.assertRaisesRegex(ValueError, 'exact_two'):
            self.validate(values)

    def test_registration_review_adoption_and_manifest_are_bound(self):
        mutations = [lambda o: o['/fixture/review'].update(accepted_storage_phase_registration=False),
                     lambda o: o['/fixture/adoption'].update(root_accepted=False),
                     lambda o: o['/fixture/adoption'].update(independent_review={'wrong': True}),
                     lambda o: o['/fixture/adoption'].update(manifest={'wrong': True}),
                     lambda o: o['/fixture/registration'].update(phase='successors_running')]
        for change in mutations:
            values = storage_fixture()
            change(values[2])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(values)

    def test_stale_source_phase_extra_role_and_missing_retirement_fail(self):
        mutations = [lambda e: e.update(observed_until=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=1)).isoformat()),
                     lambda e: e.update(source_sha256='unqualified'),
                     lambda e: e['decision'].update(phase='different'),
                     lambda e: e['observations'].update(extra=e['observations']['new_texas']),
                     lambda e: e['observations']['old_mi'].update(live=True),
                     lambda e: e['observations']['old_mi'].update(active_claims=1),
                     lambda e: e['final_ownership']['old_texas'].update(absent=False, state='S'),
                     lambda e: e['final_ownership']['new_texas'].update(process_starttime_ticks='reused')]
        for change in mutations:
            values = storage_fixture()
            change(values[1]['source_identity']['overlay']['succession_observations'][0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(values)

    def test_guard_barrier_output_queue_and_controller_bindings_fail_closed(self):
        mutations = [lambda r: r['guard'].update(healthy=False),
                     lambda r: r['guard']['alarms'].update(volume_below_floor=True),
                     lambda r: r.update(barrier_present=True),
                     lambda r: r.update(output_root='/unrelated'),
                     lambda r: r.update(queue_identity={'different': True}),
                     lambda r: r['metadata_pins'].update(controller_identity_pin={'unbound': True})]
        for change in mutations:
            values = storage_fixture()
            change(values[1]['source_identity']['overlay']['succession_observations'][0]['observations']['new_texas'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(values)

    def test_waiting_successor_must_have_no_claims_or_blocks(self):
        for field in ('claims', 'active_claims', 'persisted_blocks'):
            values = storage_fixture('texas_waiting_mi_retired')
            values[1]['source_identity']['overlay']['succession_observations'][0]['observations']['new_texas'][field] = 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(values)

    def test_retired_pid_reuse_and_extra_live_controller_are_rejected(self):
        values = storage_fixture()
        values[1]['source_identity']['overlay']['succession_observations'][0]['final_ownership']['old_mi'] = {
            'pid': 10, 'absent': False, 'state': 'Z', 'start_ticks': '9999'}
        with self.assertRaisesRegex(ValueError, 'retirement_identity'):
            self.validate(values)
        values = storage_fixture()
        values[1]['source_identity']['controllers'].append({'pid': 999, 'model': 'extra'})
        with self.assertRaisesRegex(ValueError, 'owner_set'):
            self.validate(values)



class CapacityStorageMonitorTests(unittest.TestCase):
    validate = StorageMonitorTests.validate
    def capacity_values(self):
        values = storage_fixture('mi_waiting')
        owner = values[2]['/fixture/registration']['owners']['new_mi']
        binding = {'guard_argv': ['python', 'exact_capacity_guard']}
        owner['guard'] = {'binding': {'capacity_guard': binding}}
        row = values[1]['source_identity']['overlay']['succession_observations'][0]['observations']['new_mi']
        capacity = {'status': 'healthy', 'admission_allowed': True,
                    'candidate': copy.deepcopy(owner['process']),
                    'guard': {'argv': binding['guard_argv']}}
        row['guard']['capacity_guard'] = capacity
        row['guard']['fresh_storage'] = {'capacity_guard': copy.deepcopy(capacity)}
        row['guard']['alarms']['capacity_guard_not_ready'] = False
        return values, owner, row

    def test_registered_capacity_guard_healthy_extra_alarm_is_accepted(self):
        self.validate(self.capacity_values()[0])

    def test_capacity_alarm_without_registered_binding_is_rejected(self):
        values, owner, row = self.capacity_values()
        owner.pop('guard')
        with self.assertRaises(ValueError):
            self.validate(values)

    def test_capacity_binding_status_owner_and_fresh_evidence_fail_closed(self):
        changes = [lambda o,r: o['guard']['binding'].update(capacity_guard=None),
                   lambda o,r: r['guard']['capacity_guard'].update(status='startup_held'),
                   lambda o,r: r['guard']['capacity_guard'].update(admission_allowed=False),
                   lambda o,r: r['guard']['capacity_guard']['candidate'].update(pid=999),
                   lambda o,r: r['guard']['capacity_guard']['guard'].update(argv=['different']),
                   lambda o,r: r['guard']['fresh_storage'].update(capacity_guard={}),
                   lambda o,r: r['guard']['alarms'].update(capacity_guard_not_ready=True),
                   lambda o,r: r['guard']['alarms'].pop('capacity_guard_not_ready'),
                   lambda o,r: r['guard']['alarms'].update(unrecognized=False)]
        for change in changes:
            values, owner, row = self.capacity_values()
            change(owner,row)
            with self.subTest(change=change), self.assertRaises((ValueError, KeyError)):
                self.validate(values)

if __name__ == '__main__':
    unittest.main()
