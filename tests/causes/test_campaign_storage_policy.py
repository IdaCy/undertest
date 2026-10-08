import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_storage_policy as policy
from causes.campaign_dispatcher import sha


def job(rid):
    return {'spec': {'task_id': rid}, 'variant': {'id': 'baseline'},
            'allocation': {'run_id': rid, 'continue_to_end': False, 'seed': 17}}


class StoragePolicyTests(unittest.TestCase):
    def test_reduced_root_reserve_requires_both_predecessors_retired(self):
        binding = {'floor_bytes': 80 * 1024**3, 'root_floor_bytes': 20 * 1024**3}
        owners = {role: {'observation_state': 'retired'} for role in ('old_mi', 'old_texas')}
        policy.validate_root_reserve(binding, owners)
        for role in owners:
            changed = copy.deepcopy(owners)
            changed[role]['observation_state'] = 'draining'
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'both_retired'):
                policy.validate_root_reserve(binding, changed)
        for value in (True, 19 * 1024**3, 21 * 1024**3, '21474836480'):
            binding['root_floor_bytes'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reserve_scope'):
                policy.validate_root_reserve(binding, owners)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'runs/original/mi27'
        (self.root / '.dispatcher').mkdir(parents=True)
        self.path = self.root / '.dispatcher/journal.jsonl'
        self.serial = 0

    def save(self, value):
        self.serial += 1
        return h.save_new(self.base, str(self.serial) + '.json', value)

    def journal(self, events):
        self.path.write_bytes(b''.join(h.encoded(row) + b'\n' for row in events))
        return h.pin(self.path)

    def scope(self, events):
        return {'output_root': str(self.root), 'baseline': self.journal(events),
                'mode': 'live_prefix', 'recorder_relevant': True,
                'model_aliases': ['model:current'], 'task_routes': ['task:current']}

    def test_complete_same_root_history_preserves_prefix_and_clears_counter(self):
        rows = [{'event': 'route_failure', 'route': 'model:current', 'consecutive_failures': 2}]
        scope = self.scope(rows)
        self.journal(rows + [{'event': 'route_success', 'routes': ['model:current']}])
        result = policy.read_history(scope)
        self.assertEqual(result['replay']['lifetime_route_failures'], 1)
        self.assertEqual(result['replay']['nonzero_failures'], {})
        self.assertEqual(len(result['events']), 2)

    def test_sealed_append_and_replaced_prefix_are_rejected(self):
        rows = [{'event': 'controller_started'}]
        scope = self.scope(rows)
        scope['mode'] = 'sealed'
        self.journal(rows + [{'event': 'controller_paused'}])
        with self.assertRaisesRegex(ValueError, 'sealed_storage_history_changed'):
            policy.read_history(scope)
        scope['mode'] = 'live_prefix'
        self.journal([{'event': 'controller_started', 'changed': True}])
        with self.assertRaisesRegex(ValueError, 'prefix_changed'):
            policy.read_history(scope)

    def historical(self):
        scope = self.scope([{'event': 'route_quarantined', 'route': 'recorder:old',
                             'quarantine_id': 'retained'}])
        scope['recorder_relevant'] = False
        accepted = self.save({'output_root': str(self.root), 'baseline': scope['baseline'],
            'retained_quarantines': {'recorder:old': 'retained'},
            'root_remains_excluded': True, 'requalification_performed': False})
        review = self.save({'accepted_storage_history_scope': True, 'scope': accepted})
        scope['historical_recorder_scope'] = {'evidence': accepted, 'review': review}
        return scope

    def test_historical_quarantine_is_retained_without_requalification(self):
        scope = self.historical()
        result = policy.read_history(scope)
        self.assertEqual(result['replay']['quarantines'], {'recorder:old': 'retained'})
        self.assertEqual(result['events'][0]['event'], 'route_quarantined')

    def test_unaccepted_or_changed_historical_quarantine_is_rejected(self):
        scope = self.historical()
        scope['historical_recorder_scope']['review'] = self.save({
            'accepted_storage_history_scope': False,
            'scope': scope['historical_recorder_scope']['evidence']})
        with self.assertRaisesRegex(ValueError, 'unaccepted_storage_input'):
            policy.read_history(scope)
        scope = self.historical()
        with self.path.open('ab') as f:
            f.write(h.encoded({'event': 'route_quarantined', 'route': 'recorder:old',
                              'quarantine_id': 'different'}) + b'\n')
        with self.assertRaisesRegex(ValueError, 'historical_storage_recorder_scope_changed'):
            policy.read_history(scope)

    def test_model_failure_is_not_hidden_by_historical_recorder_scope(self):
        scope = self.historical()
        with self.path.open('ab') as f:
            f.write(h.encoded({'event': 'route_failure', 'route': 'model:current',
                              'consecutive_failures': 1}) + b'\n')
        with self.assertRaisesRegex(ValueError, 'requires_requalification'):
            policy.read_history(scope)

    def test_torn_and_unresolved_histories_fail_closed(self):
        scope = self.scope([{'event': 'controller_started'}])
        with self.path.open('ab') as f:
            f.write(b'{')
        with self.assertRaisesRegex(ValueError, 'missing_torn'):
            policy.read_history(scope)
        scope = self.scope([{'event': 'unresolved_prior_attempt', 'run_id': 'old'}])
        with self.assertRaisesRegex(ValueError, 'history_unresolved'):
            policy.read_history(scope)

    def membership(self):
        jobs = [job(rid) for rid in ('new_b', 'old', 'partial', 'held', 'new_a')]
        config = {'model': 'fixture'}
        (self.root / 'old').mkdir()
        (self.root / 'partial').mkdir()
        events = [{'event': 'claimed', 'run_id': 'old', 'job_sha256': sha(jobs[1]),
                   'config_sha256': sha(config)}, {'event': 'episode_finished', 'run_id': 'old'}]
        p = {'campaign_root': str(self.base), 'model': 'mi27',
             'fixed_membership': {'jobs': self.save(jobs), 'config': self.save(config),
                'quality': self.save({'dispositions': [{'model': 'mi27', 'run_id': 'held',
                    'status': 'final_unavailable_quality_hold'}]}),
                'custody': self.save({'mi27': [{'run': 'runs/original/mi27/old'}]}),
                'roots': [{'output_root': str(self.root), 'process': {'pid': 123}}]},
             'launch': {'jobs': self.save([jobs[0], jobs[-1]]), 'config': self.save(config)}}
        return p, [{'output_root': str(self.root), 'events': events}]

    def test_membership_excludes_every_prior_attempt_and_preserves_order(self):
        p, histories = self.membership()
        with patch.object(h, 'retirement_state', return_value={'absent': True}), \
                patch.object(h, 'lock_free', return_value=True):
            result = policy.fixed_membership(p, histories)
        self.assertEqual(result['partition']['prior_attempted_unresolved'], ['partial'])
        self.assertEqual([row['allocation']['run_id'] for row in result['jobs']], ['new_b', 'new_a'])

    def test_membership_rejects_live_predecessor_and_unregistered_custody(self):
        p, histories = self.membership()
        with patch.object(h, 'retirement_state', return_value={'absent': False, 'state': 'S'}), \
                patch.object(h, 'lock_free', return_value=True):
            with self.assertRaisesRegex(ValueError, 'not_retired'):
                policy.fixed_membership(p, histories)
        p['fixed_membership']['custody'] = self.save({'mi27': [{'run': 'runs/other/mi27/old'}]})
        with self.assertRaisesRegex(ValueError, 'custody_scope_changed'):
            policy.fixed_membership(p, histories)

    def static(self):
        config = self.save({'model': 'fixture'})
        holds = self.save({'schema': 'caus.admission_holds.v1', 'holds': []})
        p = {'schema': 'causes.storage_admission_policy.v1', 'sources': policy.source_inventory(),
             'role': 'mi', 'model': 'mi27', 'workers': 8, 'max_full': 2,
             'original_config': config, 'holds': holds, 'output_root': str(self.base / 'new'),
             'pause_routing': {'fixture': 'routing'}, 'drain_marker': {'fixture': 'marker'},
             'history_scopes': [{'model_aliases': ['model:' + sha(['fixture', None])],
                                 'task_routes': ['task:' + sha(['new', None])]}],
             'resource_floor': {'memory_available_bytes': 16 * 1024**3, 'cpu_budget': 8},
             'launch': {'jobs': self.save([job('new')]), 'config': config,
                'options': {'workers': 8, 'max_full': 2, 'admission_holds': holds['path'],
                            'admission_holds_sha256': holds['sha256']},
                'storage_binding': {'output': str(self.base / 'new'), 'floor_bytes': 80 * 1024**3,
                    'admission_control': {'schema': 'causes.storage_admission_controls.v1',
                        'role': 'mi', 'pause_routing': {'fixture': 'routing'},
                        'drain_marker': {'fixture': 'marker'}}}}}
        return p

    def operator(self, p):
        ref = self.save(p)
        gate = self.save({'offline_fixture': True})
        readiness = self.save({'schema': 'causes.storage_admission_readiness.v1', 'policy': ref,
            'gates': {name: {'accepted': True, 'evidence': [gate]} for name in policy._REQUIRED_READINESS}})
        review = self.save({'accepted_storage_admission_readiness': True, 'readiness': readiness})
        return policy.StorageAdmission(ref, {'evidence': readiness, 'review': review}, {})

    def test_static_requires_bound_readiness_and_unchanged_request_config(self):
        p = self.static()
        self.operator(p).static()
        p['launch']['config'] = self.save({'model': 'different'})
        with self.assertRaisesRegex(ValueError, 'request_config_changed'):
            self.operator(p).static()

    def test_fixed_role_cannot_change_full_cap_or_add_extensions(self):
        for field in ('cap', 'extension', 'role'):
            p = self.static()
            if field == 'cap':
                p['max_full'] = p['launch']['options']['max_full'] = 4
            elif field == 'extension':
                p['launch']['options']['extension_prototypes'] = [job('extra')]
            else:
                p['model'] = 'tex9'
            with self.assertRaises(ValueError):
                self.operator(p).static()

    def test_relevant_model_and_task_routes_cannot_be_omitted(self):
        for field in ('model_aliases', 'task_routes'):
            p = self.static()
            p['history_scopes'][0][field] = []
            with self.assertRaisesRegex(ValueError, 'route_scope_incomplete'):
                self.operator(p).static()

    def test_runtime_cannot_omit_or_switch_continuous_pause_binding(self):
        for change in ('missing', 'role', 'marker'):
            p = self.static()
            controls = p['launch']['storage_binding']['admission_control']
            if change == 'missing':
                del p['launch']['storage_binding']['admission_control']
            elif change == 'role':
                controls['role'] = 'texas'
            else:
                controls['drain_marker'] = {'fixture': 'other'}
            with self.assertRaisesRegex(ValueError, 'continuous_storage_pause_coverage_required'):
                self.operator(p).static()

    def test_import_inventory_is_immutable_and_on_disk_drift_is_rejected(self):
        original = policy.source_inventory()
        changed = policy.source_inventory()
        changed['causes.campaign_handoff']['sha256'] = 'wrong'
        self.assertEqual(policy.source_inventory(), original)
        p = self.static()
        p['sources'] = changed
        with self.assertRaisesRegex(ValueError, 'policy_sources_changed'):
            self.operator(p).static()
        operator = self.operator(self.static())
        with patch.object(h, 'checked_bytes', side_effect=ValueError('handoff_input_pin_changed')):
            with self.assertRaises(ValueError):
                operator.static()

    def test_authority_binds_exact_policy_before_any_live_observation(self):
        p = self.static()
        p['launch']['options'].update(pause_file=str(self.base / 'LOCAL_PAUSE'),
                                     global_pause_file=str(self.base / 'GLOBAL_PAUSE'))
        operator = self.operator(p)
        now = dt.datetime.now(dt.timezone.utc)
        value = {'root_authorized': True, 'no_user_revocation': True,
                 'action': policy.runtime.START, 'issued_at': now.isoformat(),
                 'expires_at': (now + dt.timedelta(seconds=120)).isoformat(),
                 'policy': {'wrong': 'policy'}, 'readiness': operator.readiness_pair}
        operator.authority_ref = self.save(value)
        with patch.object(operator, 'live') as live:
            with self.assertRaisesRegex(ValueError, 'authority_scope_changed'):
                operator.authority(policy.runtime.START).check()
            live.assert_not_called()
        value['policy'] = operator.policy_ref
        operator.authority_ref = self.save(value)
        with patch.object(operator, 'live', return_value={}) as live:
            operator.authority(policy.runtime.START).check()
            live.assert_called_once()
        (self.base / 'LOCAL_PAUSE').write_text('pause')
        with patch.object(operator, 'live') as live:
            with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
                operator.authority(policy.runtime.START).check()
            live.assert_not_called()

    def test_release_requires_independently_accepted_waiting_phase(self):
        operator = self.operator(self.static())
        registration = self.save({'phase': 'successors_running'})
        pair = {'evidence': registration, 'review': self.save({
            'accepted_storage_phase_registration': True, 'registration': registration})}
        with patch.object(h, 'release_owned_barrier') as release:
            with self.assertRaisesRegex(ValueError, 'requires_waiting_phase'):
                operator.release(pair, execute=True)
            release.assert_not_called()
        pair['review'] = self.save({'accepted_storage_phase_registration': False,
                                   'registration': registration})
        with self.assertRaisesRegex(ValueError, 'unaccepted_storage_input'):
            operator.release(pair, execute=True)


if __name__ == '__main__':
    unittest.main()
