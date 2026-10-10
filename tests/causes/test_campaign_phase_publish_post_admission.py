"""Guard-held retirement preserves admitted work before remapping MI ownership."""

import copy
import datetime as dt
import fcntl
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_phase_publish as publisher
from causes import campaign_storage_identity as identity
from tests.causes import test_campaign_phase_publish_succession as succession


class PostAdmissionReturnTests(unittest.TestCase):
    save = succession.NaturalMISuccessionTests.save
    authorization = succession.NaturalMISuccessionTests.authorization
    registration_refs = succession.NaturalMISuccessionTests.registration_refs
    events = staticmethod(succession.NaturalMISuccessionTests.events)

    def setUp(self):
        # Reuse real journal, manifest, ancestry, old control and owner fixtures.
        succession.NaturalMISuccessionTests.setUp(self)
        self.barrier = self.root / 'ADMISSION_BARRIER'
        marker = h.save_new(self.root, self.barrier.name, {'fixture': 'owned admission barrier'})
        self.owner['barrier_path'] = str(self.barrier)
        self.owner['owned_barrier'] = self.save({'marker': marker,
            'stat': h.stat_identity(self.barrier), 'archived_marker': self.save(h.checked(marker))})
        succession.NaturalMISuccessionTests.retired(self)
        old_ref, old_review, old_adoption = self.registration_refs(self.old_registration)
        self.old_pointer.update(registration=old_ref, independent_review=old_review,
                                root_adoption=old_adoption)
        self.pointer.write_text(json.dumps(self.old_pointer))
        self.original_pointer_bytes = self.pointer.read_bytes()
        self.plan['previous'] = {'pin': h.pin(self.pointer), 'stat': h.stat_identity(self.pointer)}
        ancestry = self.save({'schema': 'causes.storage_mi_succession_ancestry.v1',
            'predecessor_pointer': self.plan['previous']['pin'],
            'predecessor_registration': old_ref, 'predecessor_review': old_review,
            'predecessor_adoption': old_adoption,
            'retired_owner': self.old_registration['owners']['old_mi'], 'previous': None})
        self.registration['mi_succession_ancestry'] = ancestry
        self.plan.pop('natural_mi_succession')
        self.plan.pop('interim_drain')
        self.plan.update(schema='causes.storage_phase_publication_plan.v5',
            post_admission_return={'predecessor_authorization': self.save(self.old_auth),
                                   'ancestry': ancestry})
        guard_process = {'pid': 2147483647, 'process_starttime_ticks': '999',
                         'argv': ['fixture', 'guard']}
        self.health_lock = self.root / 'HEALTH.lock'
        self.health_lock.touch()
        self.lease_pointer = self.root / 'LEASE.json'
        binding = {'trial_id': 'post-admission-fixture', 'output_root': self.owner['output_root'],
            'pause_file': str(self.mi_pause), 'health_lock_path': str(self.health_lock),
            'lease_path': str(self.lease_pointer)}
        trial = self.save({'owner_process': self.owner['process'], 'capacity_guard': binding})
        guard = self.save({'passed': True, 'barrier_release': False, 'process': guard_process,
            'candidate': self.owner['process'], 'owned_barrier': self.owner['owned_barrier'],
            'trial_plan': trial})
        intent = self.save({'schema': 'causes.capacity_guard_hold.v1', 'guard': guard_process,
            'trial_id': binding['trial_id'], 'output_root': self.owner['output_root'],
            'reasons': ['candidate_probe_failure'], 'automatic_resume': False})
        self.mi_pause.unlink()  # Replace the fixture's natural-drain marker with guard ownership.
        pause = h.save_new(self.root, self.mi_pause.name, {'intent': intent})
        self.owned_marker = {'pin': pause, 'stat': h.stat_identity(self.mi_pause), 'intent': intent}
        held = self.save({'created': True, 'intent': intent, 'marker': pause,
                          'stat': self.owned_marker['stat']})
        stop = self.save({'admission_held': True, 'automatic_retry': False,
                         'pending_endpoint': None})
        lease = {'state': 'held', 'guard': guard_process, 'candidate': self.owner['process'],
            'plan': trial, 'binding': binding, 'reason': ['candidate_probe_failure'],
            'health_lock': h.stat_identity(self.health_lock)}
        self.lease = h.save_new(self.root, 'lease_1.json', lease)
        h.save_new(self.root, self.lease_pointer.name, {'record': self.lease})
        inner = self.save({'released_owned_barrier': self.owner['owned_barrier'],
            'barrier_path': str(self.barrier), 'no_pause_marker_removed': True})
        release = self.save({'passed': True, 'automatic_retry': False, 'release': inner})
        self.barrier.unlink()
        previous_routing = self.plan['pause_routing']
        routing = h.checked(previous_routing)
        ownership = self.save({'schema': 'causes.retired_owned_pause.v1', 'role': 'mi',
            'owner_retired': True, 'marker': pause, 'stat': self.owned_marker['stat'],
            'predecessor_routing': previous_routing})
        routing['retained_owned_pauses'].append({'evidence': ownership,
            'review': self.save({'accepted_retired_owned_pause': True, 'ownership': ownership})})
        routing['predecessor_routing'] = previous_routing
        routing['successor_local_pauses']['mi'] = str(self.root / 'return_pause')
        routing['user_pause_fanout'].append(str(self.root / 'return_pause'))
        self.plan['pause_routing'] = self.save(routing)
        self.retirement.update(schema='causes.capacity_post_admission_hold_retirement_result.v1',
            plan={'registration': old_ref, 'registration_review': old_review,
                  'published_identity': self.plan['previous']['pin'], 'guard_result': guard,
                  'release_result': release},
            owned_marker=self.owned_marker, owned_barrier=self.owner['owned_barrier'],
            barrier_absent=True, release_result=release, barrier_release=inner,
            health_lock_free=True, health_lock=h.stat_identity(self.health_lock),
            hold_payloads={'HOLD_INTENT': intent, 'HOLD_RESULT': held, 'PAUSE': pause,
                           'STOPPED': stop, 'FINAL_LEASE': self.lease},
            optional_records_present={'STOPPED': True, 'FAILED': False,
                                      'FIRST_ADMISSION': False, 'WINDOW': False},
            run_directories_before=['original-a'], run_directories_after=['original-a'],
            claims_without_run_directory=['original-b'], provider_quiescence_established=False)

    def publish(self, before_observation=None):
        result_ref = self.save(self.retirement)
        self.plan['post_admission_return']['retirement'] = {
            'evidence': result_ref, 'review': self.save({
                'accepted_mi_post_admission_hold_retirement': True, 'result': result_ref,
                'journal': self.retirement['full_final_journal'],
                'owned_marker': self.retirement['owned_marker']})}
        ref, review, adoption = self.registration_refs(self.registration)
        self.plan.update(registration={'evidence': ref, 'review': review}, adoption=adoption)
        plan_ref = self.save(self.plan)
        review_ref = self.save({'accepted_storage_phase_publication_plan': True, 'plan': plan_ref})
        now = dt.datetime.now(dt.timezone.utc)
        authority = self.save({'root_authorized': True, 'no_user_revocation': True,
            'action': publisher.ACTION, 'issued_at': now.isoformat(),
            'expires_at': (now + dt.timedelta(seconds=120)).isoformat(),
            'plan': plan_ref, 'independent_review': review_ref,
            'root_adoption': self.plan['root_adoption']})
        def observed(registration):
            self.assertEqual(registration, self.registration)
            if before_observation:
                before_observation()
            return {'fixture_only': True, 'phase': registration['phase'], 'runtime_actions': False}
        # Only host observation is mocked; all files, prefixes, locks, controls,
        # authorization, manifests and atomic pointer publication are real.
        with patch.object(publisher.observation, 'observe_phase', side_effect=observed):
            return publisher.publish(plan_ref, review_ref, authority)

    def assert_refused(self, pattern, before_observation=None):
        with self.assertRaisesRegex(ValueError, pattern):
            self.publish(before_observation)
        self.assertEqual(self.pointer.read_bytes(), self.original_pointer_bytes)

    def test_running_candidate_full_publication_preserves_admitted_work(self):
        result = h.checked(self.publish())
        self.assertTrue(result['passed'])
        self.assertEqual(h.checked_bytes(result['preserved_previous']), self.original_pointer_bytes)
        _, registration = identity.current_registration(self.auth, self.manifest)
        self.assertEqual(registration, self.registration)
        self.assertEqual(h.checked(registration['mi_succession_ancestry'])['retired_owner'],
                         self.old_registration['owners']['old_mi'])
        self.assertEqual(h.pin(self.mi_pause), self.owned_marker['pin'])
        self.assertEqual(h.pin(self.journal), self.retirement['full_final_journal'])

    def test_released_waiting_phase_can_return_without_running_publication(self):
        self.old_registration['phase'] = 'mi_waiting'
        self.old_registration['owners']['new_mi']['observation_state'] = 'waiting'
        ref, review, adoption = self.registration_refs(self.old_registration)
        self.old_pointer.update(registration=ref, independent_review=review, root_adoption=adoption)
        self.pointer.write_text(json.dumps(self.old_pointer))
        self.original_pointer_bytes = self.pointer.read_bytes()
        self.plan['previous'] = {'pin': h.pin(self.pointer), 'stat': h.stat_identity(self.pointer)}
        ancestry = h.checked(self.plan['post_admission_return']['ancestry'])
        ancestry.update(predecessor_pointer=self.plan['previous']['pin'],
                        predecessor_registration=ref, predecessor_review=review,
                        predecessor_adoption=adoption)
        ancestry_ref = self.save(ancestry)
        self.plan['post_admission_return']['ancestry'] = ancestry_ref
        self.registration['mi_succession_ancestry'] = ancestry_ref
        self.retirement['plan'].update(registration=ref, registration_review=review,
                                      published_identity=self.plan['previous']['pin'])
        self.assertTrue(h.checked(self.publish())['passed'])

    def test_claim_count_cannot_disagree_with_complete_owned_journal(self):
        self.retirement['observation']['claims'] = 3
        self.assert_refused('complete_positive_retirement')

    def test_boolean_claim_count_is_not_positive_admission(self):
        self.retirement['observation']['claims'] = True
        self.assert_refused('complete_positive_retirement')

    def test_unfinished_owned_claim_is_rejected(self):
        rows = [json.loads(line) for line in self.journal.read_bytes().splitlines()]
        rows = [row for row in rows if not (row['event'] == 'episode_finished'
                                            and row['run_id'] == 'original-b')]
        self.journal.write_bytes(self.events(*rows))
        final = h.pin(self.journal)
        self.retirement['full_final_journal'] = final
        self.retirement['owner']['journal_prefix'] = final
        self.retirement['observation']['metadata_pins']['journal'] = final
        self.registration['owners']['old_mi']['journal_prefix'] = final
        self.assert_refused('complete_positive_retirement')

    def test_equal_bytes_replacement_does_not_preserve_owned_pause_stat(self):
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.mi_pause.read_bytes())
        replacement.replace(self.mi_pause)
        self.assert_refused('owned_hold_changed')

    def test_wrong_candidate_held_lease_is_rejected(self):
        lease = h.checked(self.lease)
        lease['candidate'] = {'pid': 42}
        changed = self.save(lease)
        self.retirement['hold_payloads']['FINAL_LEASE'] = changed
        self.lease_pointer.write_text(json.dumps({'record': changed}))
        self.assert_refused('guard_lease_changed')

    def test_busy_health_lock_is_rejected(self):
        with self.health_lock.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_refused('guard_lease_changed')

    def test_texas_owner_replacement_is_rejected(self):
        self.registration = copy.deepcopy(self.registration)
        self.registration['owners']['new_texas']['process']['pid'] += 1
        self.assert_refused('owner_receipt_changed')

    def test_displaced_retiree_ancestry_cannot_be_dropped(self):
        del self.registration['mi_succession_ancestry']
        self.assert_refused('ancestry_changed')

    def test_reappeared_admission_barrier_is_rejected(self):
        self.barrier.write_text('unexpected barrier')
        self.assert_refused('release_changed')

    def test_racing_user_pause_preserves_previous_pointer_and_failure_receipt(self):
        self.assert_refused('pause_blocks_handoff',
                            lambda: self.global_pause.write_text('user pause'))
        self.assertTrue((self.root / 'publication/FAILED.json').is_file())
        self.assertTrue((self.root / 'publication/NEXT.pointer.json').is_file())


if __name__ == '__main__':
    unittest.main()
