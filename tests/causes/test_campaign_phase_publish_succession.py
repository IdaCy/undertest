import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_phase_publish as publisher
from causes import campaign_storage_identity as identity
from tests.causes import test_campaign_phase_publish_return as return_tests


class NaturalMISuccessionTests(unittest.TestCase):
    def setUp(self):
        package = Path(publisher.__file__).parent.resolve()
        base = package.parent.parent
        (base / 'runs').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='.natural-succession-', dir=base / 'runs')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0
        self.global_pause, self.mi_pause, self.texas_pause = (
            self.root / name for name in ('global_pause', 'mi_pause', 'texas_pause'))
        original = self.save({'origin': 'storage_transition', 'transition_id': 'original'})
        owned = self.save({'origin': 'storage_transition', 'transition_id': 'original',
            'pin': original, 'stat': h.stat_identity(original['path']),
            'archived_marker': self.save(h.checked(original))})
        prior_pause = self.root / 'prior_mi_pause'
        prior_routing = self.save({'schema': 'causes.storage_pause_routing.v1', 'root_accepted': True,
            'transition_id': 'original', 'drain_marker': original,
            'successor_global_pause': str(self.global_pause),
            'successor_local_pauses': {'mi': str(prior_pause), 'texas': str(self.texas_pause)},
            'user_pause_fanout': [original['path'], str(self.global_pause),
                                str(prior_pause), str(self.texas_pause)]})
        prior_marker = h.save_new(self.root, prior_pause.name, {'fixture': 'retired MI pause'})
        prior_ownership = self.save({'schema': 'causes.retired_owned_pause.v1', 'role': 'mi',
            'owner_retired': True, 'marker': prior_marker, 'stat': h.stat_identity(prior_pause),
            'predecessor_routing': prior_routing})
        routing_value = h.checked(prior_routing)
        routing_value.update(schema='causes.storage_pause_routing.v2', predecessor_routing=prior_routing,
            retained_owned_pauses=[{'evidence': prior_ownership, 'review': self.save({
                'accepted_retired_owned_pause': True, 'ownership': prior_ownership})}])
        routing_value['successor_local_pauses']['mi'] = str(self.mi_pause)
        routing_value['user_pause_fanout'].append(str(self.mi_pause))
        routing = self.save(routing_value)
        outputs = {role: str(self.root / role)
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
            root = Path(outputs[role])
            (root / '.dispatcher').mkdir(parents=True)
            journal = root / '.dispatcher/journal.jsonl'
            journal.write_bytes(self.events({'event': 'controller_started', 'controller_id': role}))
            owner = {'output_root': str(root), 'process': {'pid': number,
                'process_starttime_ticks': str(number * 100), 'argv': ['fixture', role]},
                'controller_id': role, 'observation_state':
                    'running' if role.startswith('new_') else 'retired',
                'journal_prefix': h.pin(journal),
                'queue_identity': {'jobs_sha256': 'unchanged-original-jobs',
                                   'config_sha256': 'unchanged-original-config'}}
            if role.startswith('new_'):
                owner['controller_fields'] = {'pause_file': str(self.mi_pause if role == 'new_mi'
                    else self.texas_pause), 'global_pause_file': str(self.global_pause)}
                owner['guard'] = {'binding': {'output': str(root),
                    'admission_control': {'pause_routing': routing}},
                    'directory': str(self.root / (role + '_guard')), 'reporting_lock': {'fixture': role}}
                owner['launch_owner_receipt'] = self.save({'process': owner['process'],
                    'controller_id': role, 'binding': {'output': str(root),
                    'storage_binding': owner['guard']['binding']},
                    'guard_directory': owner['guard']['directory'],
                    'guard_reporting_lock': owner['guard']['reporting_lock']})
            owners[role] = owner
        self.old_registration = {'schema': 'causes.storage_controller_phase.v1',
            'phase': 'successors_running', 'identity_manifest': self.old_auth['manifest'],
            'owners': owners, 'preserved_older_history': {'fixture': 'original history and holds'}}
        old_ref, old_review, old_adoption = self.registration_refs(self.old_registration)
        self.old_pointer = {'schema': 'causes.storage_identity_pointer.v1',
            'manifest': self.old_auth['manifest'], 'registration': old_ref,
            'independent_review': old_review, 'root_adoption': old_adoption}
        h.save_new(self.root, self.pointer.name, self.old_pointer)
        self.original_pointer_bytes = self.pointer.read_bytes()
        previous = {'pin': h.pin(self.pointer), 'stat': h.stat_identity(self.pointer)}
        self.owner = owners['new_mi']
        self.journal = Path(self.owner['journal_prefix']['path'])
        self.journal.write_bytes(self.journal.read_bytes() + self.events(
            {'event': 'claimed', 'controller_id': 'new_mi', 'run_id': 'original-a'},
            {'event': 'claimed', 'controller_id': 'new_mi', 'run_id': 'original-b'}))
        boundary = h.pin(self.journal)
        journal_copy = publisher.write_bytes(self.root / 'PREFLIGHT_JOURNAL.jsonl', self.journal.read_bytes())
        drain_plan = self.save({'registration': old_ref, 'current_pointer': previous['pin'],
            'pause_path': str(self.mi_pause), 'existing_owned_drain': owned})
        preflight = self.save({'plan': drain_plan, 'mi_observation': {
            'process': self.owner['process'], 'output_root': self.owner['output_root']},
            'journal_boundary': {'pin': boundary, 'claims': 2, 'active_claims': 2},
            'journal_copy': journal_copy})
        authority = self.save({'action': 'begin_mi_natural_drain'})
        marker = {'origin': 'capacity_transition', 'transition_id': 'next',
            'owner_process': self.owner['process'], 'controller_id': self.owner['controller_id'],
            'plan': drain_plan, 'authority': authority, 'preflight': preflight}
        marker_pin = h.save_new(self.root, self.mi_pause.name, marker)
        self.owned_marker = {'origin': 'capacity_transition', 'transition_id': 'next',
            'owner_process': self.owner['process'], 'controller_id': self.owner['controller_id'],
            'pin': marker_pin, 'stat': h.stat_identity(self.mi_pause),
            'archived_marker': self.save(marker)}
        drain = self.save({'schema': 'causes.mi_natural_drain_started.v1', 'drain_requested': True,
            'signal_sent': False, 'successor_started': False, 'owned_marker': self.owned_marker,
            'plan': drain_plan, 'authority': authority, 'preflight': preflight})
        drain_pair = {'evidence': drain, 'review': self.save({
            'accepted_mi_natural_drain_actual': True, 'result': drain})}
        ancestry = self.save({'schema': 'causes.storage_mi_succession_ancestry.v1',
            'predecessor_pointer': previous['pin'], 'predecessor_registration': old_ref,
            'predecessor_review': old_review, 'predecessor_adoption': old_adoption,
            'retired_owner': owners['old_mi'], 'previous': None})
        self.manifest = copy.deepcopy(self.old_manifest)
        self.manifest['outputs'].update(old_mi=outputs['new_mi'], new_mi=str(self.root / 'next_mi'))
        self.auth = self.authorization(self.manifest)
        draining = copy.deepcopy(self.owner)
        draining.update(observation_state='draining', journal_prefix=boundary)
        self.registration = {'schema': 'causes.storage_controller_phase.v1',
            'phase': 'texas_running_mi_draining', 'identity_manifest': self.auth['manifest'],
            'owners': {'old_mi': draining, 'old_texas': owners['old_texas'],
                       'new_texas': owners['new_texas']}, 'mi_succession_ancestry': ancestry}
        self.plan = {'schema': 'causes.storage_phase_publication_plan.v4',
            'publisher': h.pin(Path(publisher.__file__).resolve()),
            'identity_authorization': self.save(self.auth), 'previous': previous,
            'pause_routing': routing, 'owned_drain': owned, 'interim_drain': drain_pair,
            'natural_mi_succession': {'predecessor_authorization': self.save(self.old_auth),
                                      'ancestry': ancestry},
            'receipt_directory': str(self.root / 'publication'),
            'root_adoption': self.save({'root_accepted': True, 'fixture': 'publication only'})}

    @staticmethod
    def events(*rows):
        return b''.join((json.dumps(row, sort_keys=True) + '\n').encode() for row in rows)

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

    def retired(self):
        self.journal.write_bytes(self.journal.read_bytes() + self.events(
            {'event': 'episode_finished', 'controller_id': 'new_mi', 'run_id': 'original-a'},
            {'event': 'episode_finished', 'controller_id': 'new_mi', 'run_id': 'original-b'},
            {'event': 'controller_paused', 'controller_id': 'new_mi'}))
        final = h.pin(self.journal)
        owner = copy.deepcopy(self.owner)
        owner.update(observation_state='retired', journal_prefix=final)
        self.registration.update(phase='texas_running_mi_retired')
        self.registration['owners']['old_mi'] = owner
        self.retirement = {'schema': 'causes.mi_retirement_metadata_result.v1',
            'passed': True, 'runtime_actions': False, 'process_signals': False, 'automatic_retry': False,
            'plan': {'registration': self.old_pointer['registration'],
                'registration_review': self.old_pointer['independent_review'],
                'published_identity': self.plan['previous']['pin'],
                'begin_drain': self.plan['interim_drain']},
            'owner': owner, 'full_final_journal': final, 'owned_marker': self.owned_marker,
            'observation': {'process': owner['process'], 'output_root': owner['output_root'],
                'metadata_pins': {'journal': final}, 'claims': 2, 'active_claims': 0,
                'live': False, 'lock_owner': None, 'drain_progress': {
                    'pause_acknowledged': True, 'new_claims_after_pause': 0}}}

    def publish(self, before_observation=None):
        if hasattr(self, 'retirement') and self.plan['schema'].endswith('.v4'):
            result = self.save(self.retirement)
            self.plan['natural_mi_succession']['retirement'] = {'evidence': result,
                'review': self.save({'accepted_mi_retirement_metadata': True, 'result': result,
                                    'journal': self.retirement['full_final_journal']})}
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
        # Only fresh host observation is simulated. Real source/identity, owner,
        # ancestry, controls, authority and atomic pointer checks all execute.
        with patch.object(publisher.observation, 'observe_phase', side_effect=observed):
            return publisher.publish(plan_ref, review_ref, authority)

    def test_full_draining_remap_preserves_current_owner_and_all_ancestry(self):
        result = h.checked(self.publish())
        self.assertTrue(result['passed'])
        self.assertFalse(result['actor_admission'])
        self.assertEqual(h.checked_bytes(result['preserved_previous']), self.original_pointer_bytes)
        _, registration = identity.current_registration(self.auth, self.manifest)
        self.assertEqual(registration, self.registration)
        ancestry = h.checked(registration['mi_succession_ancestry'])
        self.assertEqual(h.checked(ancestry['predecessor_registration']), self.old_registration)
        self.assertEqual(ancestry['retired_owner'], self.old_registration['owners']['old_mi'])
        self.assertEqual(h.pin(self.mi_pause), self.owned_marker['pin'])

    def test_direct_retirement_accepts_nonzero_completed_claims(self):
        self.retired()
        result = h.checked(self.publish())
        self.assertTrue(result['passed'])
        self.assertEqual(result['phase'], 'texas_running_mi_retired')
        self.assertEqual(identity.current_registration(self.auth, self.manifest)[1], self.registration)

    def test_existing_v2_finishes_drain_but_cannot_drop_ancestry(self):
        self.publish()
        self.plan.update(schema='causes.storage_phase_publication_plan.v2',
            previous={'pin': h.pin(self.pointer), 'stat': h.stat_identity(self.pointer)},
            receipt_directory=str(self.root / 'retired_publication'))
        del self.plan['natural_mi_succession']
        self.retired()
        ancestry = self.registration.pop('mi_succession_ancestry')
        with self.assertRaisesRegex(ValueError, 'mi_ancestry_changed'):
            self.publish()
        self.registration['mi_succession_ancestry'] = ancestry
        self.assertTrue(h.checked(self.publish())['passed'])

    def test_racing_user_pause_preserves_pointer_and_uncertainty_receipts(self):
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.publish(lambda: self.texas_pause.write_text('user pause'))
        self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)
        self.assertTrue((self.root / 'publication' / 'FAILED.json').is_file())
        self.assertTrue((self.root / 'publication' / 'NEXT.pointer.json').is_file())

    def test_fresh_observation_failure_never_publishes_a_retired_assumption(self):
        self.retired()
        def still_live():
            raise ValueError('storage_previous_owner_not_retired')
        with self.assertRaisesRegex(ValueError, 'owner_not_retired'):
            self.publish(still_live)
        self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)
        self.assertTrue((self.root / 'publication' / 'FAILED.json').is_file())
        self.assertFalse((self.root / 'publication' / 'NEXT.pointer.json').exists())

    def test_retained_older_pause_is_still_mandatory(self):
        (self.root / 'prior_mi_pause').unlink()
        with self.assertRaises(FileNotFoundError):
            self.publish()
        self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)

    def test_replacing_owned_marker_with_equal_bytes_is_rejected(self):
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.mi_pause.read_bytes())
        replacement.replace(self.mi_pause)
        with self.assertRaisesRegex(ValueError, 'marker_identity_changed'):
            self.publish()
        self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)

    def test_displaced_retiree_and_its_journal_cannot_be_lost(self):
        del self.registration['mi_succession_ancestry']
        with self.assertRaisesRegex(ValueError, 'ancestry_changed'):
            self.publish()
        self.registration['mi_succession_ancestry'] = self.plan['natural_mi_succession']['ancestry']
        Path(self.old_registration['owners']['old_mi']['journal_prefix']['path']).write_text('replaced\n')
        with self.assertRaisesRegex(ValueError, 'handoff_input_pin_changed'):
            self.publish()

    def test_queue_and_texas_bindings_cannot_change(self):
        self.registration = copy.deepcopy(self.registration)
        self.registration['owners']['old_mi']['queue_identity']['jobs_sha256'] = 'different-jobs'
        with self.assertRaisesRegex(ValueError, 'owner_mapping_changed'):
            self.publish()
        self.registration['owners']['old_mi']['queue_identity'] = self.owner['queue_identity']
        self.registration['owners']['old_texas']['process']['pid'] += 1
        with self.assertRaisesRegex(ValueError, 'predecessor_or_routing_changed'):
            self.publish()

    def test_remap_cannot_change_unrelated_manifest_outputs(self):
        changed = copy.deepcopy(self.manifest)
        changed['outputs']['old_texas'] = str(self.root / 'replacement_texas')
        self.auth = self.authorization(changed)
        self.plan['identity_authorization'] = self.save(self.auth)
        self.registration['identity_manifest'] = self.auth['manifest']
        self.registration['owners']['old_texas']['output_root'] = changed['outputs']['old_texas']
        with self.assertRaisesRegex(ValueError, 'manifest_scope_changed'):
            self.publish()

    def test_owner_prefix_cannot_skip_existing_claims(self):
        self.registration['owners']['old_mi']['journal_prefix'] = self.owner['journal_prefix']
        with self.assertRaisesRegex(ValueError, 'owner_mapping_changed'):
            self.publish()

    def test_retired_branch_requires_actual_complete_natural_retirement(self):
        self.retired()
        original = copy.deepcopy(self.retirement)
        for change in (lambda r: r['observation'].update(active_claims=1),
                       lambda r: r['observation'].update(live=True),
                       lambda r: r['observation'].update(claims=False),
                       lambda r: r['observation']['drain_progress'].update(new_claims_after_pause=1),
                       lambda r: r['plan'].update(registration={}),
                       lambda r: r.update(process_signals=True)):
            self.retirement = copy.deepcopy(original)
            change(self.retirement)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.publish()
            self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)

    def test_rebound_drain_review_or_implicit_mode_cannot_remap(self):
        self.plan['interim_drain']['review'] = self.save({
            'accepted_mi_natural_drain_actual': True, 'result': {}})
        with self.assertRaisesRegex(ValueError, 'not_independently_accepted'):
            self.publish()
        self.plan['schema'] = 'causes.storage_phase_publication_plan.v2'
        with self.assertRaisesRegex(ValueError, 'pointer_changed'):
            self.publish()

    def test_later_zero_admission_return_must_append_existing_ancestry(self):
        fixture = return_tests.CapacityReturnPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prior = fixture.save({'fixture': 'previously accepted ancestry'})
        fixture.old_registration['mi_succession_ancestry'] = prior
        old_owner = fixture.old_registration['owners']['old_mi']
        output = Path(old_owner['output_root'])
        (output / '.dispatcher').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, output.parent)
        journal = output / '.dispatcher/journal.jsonl'
        journal.write_bytes(self.events({'event': 'controller_paused'}))
        old_owner['journal_prefix'] = h.pin(journal)
        ref, review, adoption = fixture.registration_refs(fixture.old_registration)
        fixture.old_pointer.update(registration=ref, independent_review=review, root_adoption=adoption)
        fixture.pointer.write_text(json.dumps(fixture.old_pointer))
        fixture.original_pointer_bytes = fixture.pointer.read_bytes()
        fixture.previous = {'pin': h.pin(fixture.pointer), 'stat': h.stat_identity(fixture.pointer)}
        fixture.plan['previous'] = fixture.previous
        fixture.retirement['plan'].update(registration=ref, registration_review=review,
                                          published_identity=fixture.previous['pin'])
        with self.assertRaisesRegex(ValueError, 'ancestry_required'):
            fixture.publish()
        ancestry = fixture.save({'schema': 'causes.storage_mi_succession_ancestry.v1',
            'predecessor_pointer': fixture.previous['pin'], 'predecessor_registration': ref,
            'predecessor_review': review, 'predecessor_adoption': adoption,
            'retired_owner': old_owner, 'previous': prior})
        fixture.plan['capacity_return']['ancestry'] = ancestry
        with self.assertRaisesRegex(ValueError, 'ancestry_changed'):
            fixture.publish()
        fixture.registration['mi_succession_ancestry'] = ancestry
        self.assertTrue(h.checked(fixture.publish())['passed'])


if __name__ == '__main__':
    unittest.main()
