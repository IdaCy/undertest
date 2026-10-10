import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_storage_policy as policy


class CapacityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0

    def saved(self, value):
        self.serial += 1
        return h.save_new(self.root, f'{self.serial}.json', value)

    def pair(self, value, flag, key):
        evidence = self.saved(value)
        return {'evidence': evidence, 'review': self.saved({flag: True, key: evidence})}

    def fixture(self):
        criteria = self.pair({'invariants': {
            'workers': 8, 'qualified_max_full': 2, 'candidate_max_full': 4,
            'provider_restart': False, 'extra_workers': False,
            'no_replay_or_replacement': True, 'fixed_queue_only': True}},
            'accepted_prospective_operational_criteria', 'criteria')
        config = self.saved({'model': 'fixture', 'sampling': {'temperature': 0.5}})
        provider = {'pid': 10, 'process_starttime_ticks': '20', 'argv': ['server', '--cache', '48']}
        declaration = self.saved({'model_key': 'mi27', 'owner': {'workers': 8, 'max_full': 2},
            'required_pins': [config], 'processes': [{'role': 'provider', **provider}]})
        summary = self.saved({'supported': True})
        reference = {'evidence': summary, 'review': self.saved({
            'accepted_capacity_reference': True, 'summary': summary,
            'criteria': criteria['evidence'], 'declaration': declaration})}
        previous = {'observation_state': 'retired', 'output_root': str(self.root / 'old'),
                    'process': {'pid': 11, 'process_starttime_ticks': '21', 'argv': ['old-owner']}}
        barrier = self.saved({'output_root': str(self.root / 'new')})
        adoption = self.saved({'adopted_prospective_operational_criteria': True,
                               'criteria': criteria['evidence']})
        operating = self.pair({'schema': 'causes.capacity_operational_binding_addendum.v1',
            'criteria': criteria['evidence'], 'criteria_adoption': adoption,
            'candidate': {'workers': 8, 'max_full': 4, 'output_namespace': str(self.root / 'new'),
                'sole_hold_path': str(self.root / 'LOCAL_PAUSE'), 'startup_barrier': str(self.root / 'BARRIER')},
            'return_to_qualified': {'workers': 8, 'max_full': 2, 'output_namespace': str(self.root / 'return'),
                'sole_hold_path': str(self.root / 'RETURN_PAUSE'), 'startup_barrier': str(self.root / 'RETURN_BARRIER')},
            'preserved_controls': {'global': str(self.root / 'GLOBAL_PAUSE')}},
            'accepted_capacity_operational_binding_addendum', 'addendum')
        operating['adoption'] = self.saved({'root_adopted_capacity_operational_binding_addendum': True,
            'addendum': operating['evidence'], 'independent_review': operating['review'],
            'criteria': criteria['evidence']})
        return {'schema': 'causes.storage_capacity_admission_policy.v1',
            'role': 'mi', 'model': 'mi27', 'workers': 8, 'max_full': 4,
            'output_root': str(self.root / 'new'), 'provider': {'process': provider},
            'predecessor_observations': {'old_mi': previous},
            'fixed_membership': {'roots': [{'output_root': previous['output_root'],
                                          'process': previous['process']}]},
            'launch': {'config': config, 'owned_barrier': barrier,
                'options': {'pause_file': str(self.root / 'LOCAL_PAUSE'),
                            'global_pause_file': str(self.root / 'GLOBAL_PAUSE'),
                            'start_barrier_file': str(self.root / 'BARRIER')}, 'storage_binding': {
                'capacity_guard': {'trial_id': 'trial', 'output_root': str(self.root / 'new'),
                    'pause_file': str(self.root / 'LOCAL_PAUSE'),
                    'owned_startup_barrier': barrier, 'criteria': copy.deepcopy(criteria),
                    'criteria_adoption': adoption, 'reference': copy.deepcopy(reference)}}},
            'capacity_trial': {'trial_id': 'trial', 'mode': 'candidate',
                'criteria': criteria, 'criteria_adoption': adoption, 'reference': reference,
                'operational_bindings': operating,
                'reference_provider_role': 'provider'}}

    def test_candidate_requires_both_reviewed_reference_and_adopted_criteria(self):
        p = self.fixture()
        self.assertEqual(policy.capacity_scope(p), p['capacity_trial'])
        for field in ('reference', 'criteria'):
            bad = copy.deepcopy(p)
            ref = bad['capacity_trial'][field]
            value = h.checked(ref['review'])
            flag = 'accepted_capacity_reference' if field == 'reference' else 'accepted_prospective_operational_criteria'
            value[flag] = False
            ref['review'] = self.saved(value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'unaccepted_storage_input'):
                policy.capacity_scope(bad)
        p['capacity_trial']['criteria_adoption'] = self.saved({
            'adopted_prospective_operational_criteria': False})
        with self.assertRaisesRegex(ValueError, 'criteria_not_adopted'):
            policy.capacity_scope(p)

    def test_reference_for_another_workload_or_provider_cannot_qualify_candidate(self):
        original = self.fixture()
        for change in ('config', 'provider', 'model', 'reference_full', 'criteria'):
            p = copy.deepcopy(original)
            reference = p['capacity_trial']['reference']
            review = h.checked(reference['review'])
            declaration = h.checked(review['declaration'])
            if change == 'config':
                p['launch']['config'] = self.saved({'model': 'other'})
            elif change == 'provider':
                p['provider']['process']['argv'][-1] = '64'
            elif change == 'model':
                declaration['model_key'] = 'other'
            elif change == 'reference_full':
                declaration['owner']['max_full'] = 4
            else:
                review['criteria'] = self.saved({'invariants': {}})
            review['declaration'] = self.saved(declaration)
            reference['review'] = self.saved(review)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'capacity_reference_'):
                policy.capacity_scope(p)

    def test_only_eight_total_four_full_candidate_is_permitted(self):
        for field, value in [('workers', 9), ('max_full', 6), ('role', 'texas'), ('model', 'tex9')]:
            p = self.fixture()
            p[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                policy.capacity_scope(p)

    def test_retired_predecessor_must_be_in_exact_fixed_inventory(self):
        for change in ('live', 'omitted', 'identity', 'same_output'):
            p = self.fixture()
            previous = p['predecessor_observations']['old_mi']
            if change == 'live':
                previous['observation_state'] = 'running'
            elif change == 'omitted':
                p['fixed_membership']['roots'] = []
            elif change == 'identity':
                p['fixed_membership']['roots'][0]['process'] = {'pid': 99}
            else:
                previous['output_root'] = p['output_root']
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'predecessor_not_preserved'):
                policy.capacity_scope(p)

    def test_guard_from_another_trial_output_or_barrier_is_refused(self):
        for key, value in [('trial_id', 'other'), ('output_root', str(self.root / 'other')),
                           ('owned_startup_barrier', {}), ('criteria', {}),
                           ('criteria_adoption', {}), ('reference', {}),
                           ('pause_file', str(self.root / 'FOREIGN_PAUSE'))]:
            p = self.fixture()
            p['launch']['storage_binding']['capacity_guard'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'guard_binding_changed'):
                policy.capacity_scope(p)

        for key in ('global_pause_file', 'start_barrier_file'):
            p = self.fixture()
            p['launch']['options']['pause_file'] = p['launch']['options'][key]
            p['launch']['storage_binding']['capacity_guard']['pause_file'] = p['launch']['options'][key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'capacity_operational_target_changed'):
                policy.capacity_scope(p)

    def test_qualified_return_is_cap2_without_candidate_guard(self):
        p = self.fixture()
        p['capacity_trial']['mode'] = 'return_to_qualified'
        p['max_full'] = 2
        p['output_root'] = str(self.root / 'return')
        p['launch']['options']['pause_file'] = str(self.root / 'RETURN_PAUSE')
        p['launch']['options']['start_barrier_file'] = str(self.root / 'RETURN_BARRIER')
        with self.assertRaisesRegex(ValueError, 'must_not_reuse_candidate_guard'):
            policy.capacity_scope(p)
        p['launch']['storage_binding']['capacity_guard'] = None
        with self.assertRaisesRegex(ValueError, 'must_not_reuse_candidate_guard'):
            policy.capacity_scope(p)
        del p['launch']['storage_binding']['capacity_guard']
        self.assertEqual(policy.capacity_scope(p)['mode'], 'return_to_qualified')
        p['max_full'] = 4
        with self.assertRaisesRegex(ValueError, 'capacity_limit_not_approved'):
            policy.capacity_scope(p)

    def test_operational_paths_need_the_exact_adopted_addendum(self):
        for kind in ('output', 'pause', 'barrier', 'global', 'adoption'):
            p = self.fixture()
            if kind == 'output':
                p['output_root'] = str(self.root / 'unreviewed_output')
            elif kind == 'adoption':
                p['capacity_trial']['operational_bindings']['adoption'] = self.saved({
                    'root_adopted_capacity_operational_binding_addendum': False})
            else:
                key = {'pause': 'pause_file', 'barrier': 'start_barrier_file', 'global': 'global_pause_file'}[kind]
                p['launch']['options'][key] = str(self.root / 'unreviewed_control')
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'capacity_operational_'):
                policy.capacity_scope(p)

    def test_legacy_policy_cannot_smuggle_new_capacity_authorization(self):
        p = self.fixture()
        p['schema'] = 'causes.storage_admission_policy.v1'
        with self.assertRaisesRegex(ValueError, 'explicit_policy_schema'):
            policy.capacity_scope(p)
        del p['capacity_trial']
        with self.assertRaisesRegex(ValueError, 'explicit_policy_schema'):
            policy.capacity_scope(p)
        del p['launch']['storage_binding']['capacity_guard']
        self.assertIsNone(policy.capacity_scope(p))

    def test_prospective_criteria_do_not_authorize_a_runtime_transition(self):
        p = self.fixture()
        p['launch']['options'] = {'pause_file': str(self.root / 'LOCAL_PAUSE'),
                                  'global_pause_file': str(self.root / 'GLOBAL_PAUSE')}
        operator = policy.StorageAdmission(self.saved(p), {'fixture': 'readiness'}, {})
        now = dt.datetime.now(dt.timezone.utc)
        value = {'root_authorized': True, 'no_user_revocation': True,
                 'action': policy.runtime.START, 'issued_at': now.isoformat(),
                 'expires_at': (now + dt.timedelta(seconds=120)).isoformat(),
                 'policy': operator.policy_ref, 'readiness': operator.readiness_pair}
        for addition in ({}, {'authorized_capacity_transition': True, 'capacity_trial': {}}):
            operator.authority_ref = self.saved({**value, **addition})
            with self.subTest(addition=addition), patch.object(operator, 'live') as live:
                with self.assertRaisesRegex(ValueError, 'capacity_runtime_authority_required'):
                    operator.authority(policy.runtime.START).check()
                live.assert_not_called()
        operator.authority_ref = self.saved({**value, 'authorized_capacity_transition': True,
                                             'capacity_trial': p['capacity_trial']})
        with patch.object(operator, 'live', return_value={}) as live:
            operator.authority(policy.runtime.START).check()
            live.assert_called_once()


if __name__ == '__main__':
    unittest.main()
