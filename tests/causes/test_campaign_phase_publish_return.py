import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_phase_publish as publisher
from causes import campaign_storage_identity as identity


class CapacityReturnPublicationTests(unittest.TestCase):
    def setUp(self):
        package = Path(publisher.__file__).parent.resolve()
        base = package.parent.parent
        temporary = tempfile.TemporaryDirectory(prefix='.return-publication-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0
        self.global_pause, self.texas_pause, self.return_pause = (
            self.root / name for name in ('global_pause', 'texas_pause', 'return_pause'))
        candidate_pause = self.root / 'candidate_pause'
        original = self.save({'origin': 'storage_transition', 'transition_id': 'original'})
        owned_drain = self.save({'origin': 'storage_transition', 'transition_id': 'original',
            'pin': original, 'stat': h.stat_identity(original['path']),
            'archived_marker': self.save(h.checked(original))})
        routing = {'schema': 'causes.storage_pause_routing.v1', 'root_accepted': True,
            'transition_id': 'original', 'drain_marker': original,
            'successor_global_pause': str(self.global_pause),
            'successor_local_pauses': {'mi': str(candidate_pause), 'texas': str(self.texas_pause)},
            'user_pause_fanout': [original['path'], str(self.global_pause),
                                str(candidate_pause), str(self.texas_pause)]}
        previous_routing = self.save(routing)
        outputs = {role: str(base / 'runs' / (self.root.name + role) /
                            ('mi27' if role.endswith('_mi') else 'tex9'))
                   for role in ('old_mi', 'old_texas', 'new_mi', 'new_texas')}
        self.pointer = self.root / 'CURRENT.json'
        self.old_manifest = {'schema': 'causes.storage_identity_manifest.v1',
            'campaign_root': str(base), 'pythonpath': str(package.parent),
            'pointer': str(self.pointer), 'outputs': outputs,
            'python': h.pin(Path(sys.executable).resolve()),
            'source_inventory': {p.name: h.pin(p) for p in package.glob('*.py')}}
        self.old_auth = self.authorization(self.old_manifest)
        owners = {}
        for number, role in enumerate(outputs, 1):
            owner = {'output_root': outputs[role], 'process': {'pid': number,
                'process_starttime_ticks': str(100 * number), 'argv': ['fixture', role]},
                'controller_id': role, 'observation_state':
                    'waiting' if role == 'new_mi' else 'running' if role == 'new_texas' else 'retired'}
            if role.startswith('new_'):
                owner['controller_fields'] = {'pause_file': str(candidate_pause if role == 'new_mi'
                        else self.texas_pause), 'global_pause_file': str(self.global_pause)}
                owner['guard'] = {'binding': {'output': outputs[role],
                    'admission_control': {'pause_routing': previous_routing}},
                    'directory': str(self.root / (role + '_guard')), 'reporting_lock': {'fixture': role}}
                owner['launch_owner_receipt'] = self.save({'process': owner['process'],
                    'controller_id': role, 'binding': {'output': outputs[role],
                    'storage_binding': owner['guard']['binding']},
                    'guard_directory': owner['guard']['directory'],
                    'guard_reporting_lock': owner['guard']['reporting_lock']})
            owners[role] = owner
        candidate = owners['new_mi']
        barrier = self.save({'fixture': 'owned barrier'})
        candidate['barrier_path'] = barrier['path']
        candidate['owned_barrier'] = self.save({'marker': barrier,
            'stat': h.stat_identity(barrier['path']), 'archived_marker': self.save(h.checked(barrier))})
        candidate['journal_prefix'] = self.save({'fixture': 'initial journal'})
        self.old_registration = {'schema': 'causes.storage_controller_phase.v1', 'phase': 'mi_waiting',
            'identity_manifest': self.old_auth['manifest'], 'owners': owners}
        old_ref, old_review, old_adoption = self.registration_refs(self.old_registration)
        self.old_pointer = {'schema': 'causes.storage_identity_pointer.v1',
            'manifest': self.old_auth['manifest'], 'registration': old_ref,
            'independent_review': old_review, 'root_adoption': old_adoption}
        h.save_new(self.root, self.pointer.name, self.old_pointer)
        self.original_pointer_bytes = self.pointer.read_bytes()
        self.previous = {'pin': h.pin(self.pointer), 'stat': h.stat_identity(self.pointer)}
        final_journal = self.save({'fixture': 'final zero-claim journal'})
        retired = copy.deepcopy(candidate)
        retired.update(observation_state='retired', journal_prefix=final_journal)
        guard_process = {'pid': 90, 'process_starttime_ticks': '900', 'argv': ['fixture', 'guard']}
        trial = self.save({'owner_process': candidate['process'], 'capacity_guard': {'trial_id': 'trial'}})
        guard = self.save({'process': guard_process, 'candidate': candidate['process'],
                          'trial_plan': trial, 'passed': True, 'barrier_release': False})
        intent = self.save({'schema': 'causes.capacity_guard_hold.v1', 'guard': guard_process,
            'output_root': candidate['output_root'], 'trial_id': 'trial',
            'reasons': ['candidate_first_admission_not_observed'], 'automatic_resume': False})
        marker = h.save_new(self.root, candidate_pause.name, {'intent': intent})
        self.retirement = {'schema': 'causes.capacity_startup_hold_retirement_result.v1',
            'passed': True, 'runtime_actions': False, 'process_signals': False, 'automatic_retry': False,
            'plan': {'registration': old_ref, 'registration_review': old_review,
                     'published_identity': self.previous['pin'], 'guard_result': guard},
            'owner': retired, 'full_final_journal': final_journal,
            'run_directories_before': [], 'run_directories_after': [],
            'observation': {'claims': 0, 'active_claims': 0, 'live': False, 'lock_owner': None,
                'process': candidate['process'], 'output_root': candidate['output_root'],
                'drain_progress': {'pause_acknowledged': True, 'new_claims_after_pause': 0}},
            'forbidden_records_present': {'FIRST_ADMISSION': False, 'WINDOW': False, 'FAILED': False},
            'barrier_verified': True, 'health_lock_free': True, 'owned_barrier': candidate['owned_barrier'],
            'owned_marker': {'pin': marker, 'stat': h.stat_identity(candidate_pause), 'intent': intent}}
        ownership = self.save({'schema': 'causes.retired_owned_pause.v1', 'role': 'mi',
            'owner_retired': True, 'marker': marker, 'stat': h.stat_identity(candidate_pause),
            'predecessor_routing': previous_routing})
        routing = copy.deepcopy(routing)
        routing.update(schema='causes.storage_pause_routing.v2', predecessor_routing=previous_routing,
            retained_owned_pauses=[{'evidence': ownership, 'review': self.save({
                'accepted_retired_owned_pause': True, 'ownership': ownership})}])
        routing['successor_local_pauses']['mi'] = str(self.return_pause)
        routing['user_pause_fanout'].append(str(self.return_pause))
        self.manifest = copy.deepcopy(self.old_manifest)
        self.manifest['outputs'].update(old_mi=outputs['new_mi'],
            new_mi=str(base / 'runs' / (self.root.name + 'return') / 'mi27'))
        self.auth = self.authorization(self.manifest)
        self.registration = {'schema': 'causes.storage_controller_phase.v1',
            'phase': 'texas_running_mi_retired', 'identity_manifest': self.auth['manifest'],
            'owners': {'old_mi': retired, 'old_texas': owners['old_texas'], 'new_texas': owners['new_texas']}}
        self.plan = {'schema': 'causes.storage_phase_publication_plan.v3',
            'publisher': h.pin(Path(publisher.__file__).resolve()),
            'identity_authorization': self.save(self.auth), 'previous': self.previous,
            'pause_routing': self.save(routing), 'owned_drain': owned_drain,
            'capacity_return': {'predecessor_authorization': self.save(self.old_auth)},
            'receipt_directory': str(self.root / 'publication'),
            'root_adoption': self.save({'root_accepted': True, 'fixture': 'publication authority'})}

    def save(self, value):
        self.serial += 1
        return h.save_new(self.root, f'record_{self.serial}.json', value)

    def authorization(self, manifest):
        ref = self.save(manifest)
        return {'schema': 'causes.storage_identity_authorization.v1',
            'overlay_module': h.pin(Path(identity.__file__).resolve()), 'manifest': ref,
            'manifest_review': self.save({'accepted_storage_identity_manifest': True, 'manifest': ref})}

    def registration_refs(self, registration):
        ref = self.save(registration)
        review = self.save({'accepted_storage_phase_registration': True, 'registration': ref})
        return ref, review, self.save({'root_accepted': True, 'registration': ref,
            'independent_review': review, 'manifest': registration['identity_manifest']})

    def publish(self, before_observation=None):
        result_ref = self.save(self.retirement)
        self.plan['capacity_return']['retirement'] = {'evidence': result_ref, 'review': self.save({
            'accepted_capacity_startup_hold_retirement': True, 'result': result_ref})}
        ref, review, adoption = self.registration_refs(self.registration)
        self.plan.update(registration={'evidence': ref, 'review': review}, adoption=adoption)
        plan_ref = self.save(self.plan)
        review_ref = self.save({'accepted_storage_phase_publication_plan': True, 'plan': plan_ref})
        now = dt.datetime.now(dt.timezone.utc)
        authority = self.save({'root_authorized': True, 'no_user_revocation': True,
            'action': publisher.ACTION, 'issued_at': now.isoformat(),
            'expires_at': (now + dt.timedelta(seconds=120)).isoformat(),
            'plan': plan_ref, 'independent_review': review_ref, 'root_adoption': self.plan['root_adoption']})
        def observed(registration):
            self.assertEqual(registration, self.registration)
            if before_observation:
                before_observation()
            return {'fixture_only': True, 'phase': registration['phase'], 'runtime_actions': False}
        # Only the live process/mount observation is simulated. Manifest, owner,
        # pause, authority, old/new pointer validation and atomic publication are real.
        with patch.object(publisher.observation, 'observe_phase', side_effect=observed):
            return publisher.publish(plan_ref, review_ref, authority)

    def test_full_return_publication_preserves_old_manifest_pointer(self):
        result = h.checked(self.publish())
        self.assertTrue(result['passed'])
        self.assertEqual(h.checked_bytes(result['preserved_previous']), self.original_pointer_bytes)
        pointer, registration = identity.current_registration(self.auth, self.manifest)
        self.assertEqual(registration, self.registration)
        self.assertEqual(pointer['manifest'], self.auth['manifest'])
        with self.assertRaisesRegex(ValueError, 'pointer_changed'):
            identity.current_registration(self.old_auth, self.old_manifest)

    def test_attempted_or_unretired_candidate_is_rejected(self):
        for change in (lambda r: r['observation'].update(claims=1),
                       lambda r: r['observation'].update(claims=False),
                       lambda r: r['observation'].update(live=True),
                       lambda r: r.update(run_directories_after=['attempt']),
                       lambda r: r['forbidden_records_present'].update(WINDOW=0)):
            original = copy.deepcopy(self.retirement)
            change(self.retirement)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'zero_admission'):
                self.publish()
            self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)
            self.retirement = original

    def test_texas_owner_cannot_be_replaced_during_return(self):
        self.registration = copy.deepcopy(self.registration)
        self.registration['owners']['old_texas']['process']['pid'] = 300
        with self.assertRaisesRegex(ValueError, 'owner_mapping_changed'):
            self.publish()

    def test_unrelated_manifest_change_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['outputs']['old_texas'] += '_changed'
        self.auth = self.authorization(manifest)
        self.plan['identity_authorization'] = self.save(self.auth)
        self.registration['identity_manifest'] = self.auth['manifest']
        self.registration['owners']['old_texas']['output_root'] = manifest['outputs']['old_texas']
        with self.assertRaisesRegex(ValueError, 'manifest_scope_changed'):
            self.publish()

    def test_racing_user_pause_preserves_previous_pointer_and_receipts(self):
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.publish(lambda: self.global_pause.write_text('user pause'))
        self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)
        self.assertTrue((self.root / 'publication' / 'FAILED.json').is_file())
        self.assertTrue((self.root / 'publication' / 'NEXT.pointer.json').is_file())

    def test_other_phase_and_implicit_mode_are_rejected(self):
        self.registration['phase'] = 'successors_running'
        with self.assertRaisesRegex(ValueError, 'phase_scope_changed'):
            self.publish()
        self.registration['phase'] = 'texas_running_mi_retired'
        self.plan['schema'] = 'causes.storage_phase_publication_plan.v1'
        with self.assertRaisesRegex(ValueError, 'pointer_changed'):
            self.publish()


if __name__ == '__main__':
    unittest.main()
