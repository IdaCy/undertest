import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from causes import campaign_handoff as h
from causes import campaign_phase_publish as publisher


class InterimPhaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'controls').mkdir()
        old = self.root / 'controls' / 'old'
        old.mkdir()
        self.mi, self.global_pause, self.texas = (old / n for n in ('mi', 'global', 'texas'))
        original = {'origin': 'storage_transition', 'transition_id': 'old'}
        original_pin = h.save_new(self.root, 'PAUSE', original)
        original_owned = {'origin': 'storage_transition', 'transition_id': 'old',
                          'pin': original_pin, 'stat': h.stat_identity(original_pin['path']),
                          'archived_marker': h.save_new(self.root, 'old_archive', original)}
        old_owned = h.save_new(self.root, 'original_owned', original_owned)
        routing = {'schema': 'causes.storage_pause_routing.v1', 'root_accepted': True,
                   'transition_id': 'old', 'drain_marker': original_pin,
                   'successor_global_pause': str(self.global_pause),
                   'successor_local_pauses': {'mi': str(self.mi), 'texas': str(self.texas)},
                   'user_pause_fanout': [original_pin['path'], str(self.global_pause),
                                       str(self.mi), str(self.texas)]}
        self.routing = h.save_new(self.root, 'routing', routing)
        process = {'pid': 42, 'process_starttime_ticks': '123', 'argv': ['python', 'owner']}
        self.owner = {'process': process, 'controller_id': 'c1', 'observation_state': 'draining',
                      'controller_fields': {'pause_file': str(self.mi),
                                            'global_pause_file': str(self.global_pause)},
                      'guard': {'binding': {'admission_control': {'pause_routing': self.routing}}}}
        self.registration = {'phase': 'texas_running_mi_draining',
                             'owners': {'old_mi': self.owner,
                                        'old_texas': {'observation_state': 'retired'},
                                        'new_texas': {'observation_state': 'running',
                                          'controller_fields': {'pause_file': str(self.texas),
                                            'global_pause_file': str(self.global_pause)}}}}
        drain_plan = h.save_new(self.root, 'drain_plan',
                               {'pause_path': str(self.mi), 'existing_owned_drain': old_owned})
        authority = h.save_new(self.root, 'drain_authority', {'action': 'begin_mi_natural_drain'})
        preflight = h.save_new(self.root, 'preflight', {'claims': 3})
        marker = {'origin': 'capacity_transition', 'transition_id': 'next',
                  'owner_process': process, 'controller_id': 'c1', 'plan': drain_plan,
                  'authority': authority, 'preflight': preflight}
        marker_pin = h.save_new(self.mi.parent, self.mi.name, marker)
        self.owned = {'origin': 'capacity_transition', 'transition_id': 'next',
                      'owner_process': process, 'controller_id': 'c1', 'pin': marker_pin,
                      'stat': h.stat_identity(self.mi),
                      'archived_marker': h.save_new(self.root, 'mi_archive', marker)}
        result = {'schema': 'causes.mi_natural_drain_started.v1', 'drain_requested': True,
                  'signal_sent': False, 'successor_started': False, 'owned_marker': self.owned,
                  'plan': drain_plan, 'authority': authority, 'preflight': preflight}
        result_pin = h.save_new(self.root, 'drain_result', result)
        review = h.save_new(self.root, 'drain_review',
                           {'accepted_mi_natural_drain_actual': True, 'result': result_pin})
        self.plan = {'schema': 'causes.storage_phase_publication_plan.v2',
                     'pause_routing': self.routing, 'owned_drain': old_owned,
                     'interim_drain': {'evidence': result_pin, 'review': review}}

    def test_owned_current_marker_does_not_mask_live_controls(self):
        paths = publisher.publication_controls(self.plan, self.registration)
        self.assertEqual(paths, (self.global_pause, self.texas))
        self.assertEqual(h.pin(self.mi), self.owned['pin'])
        self.assertEqual(h.stat_identity(self.mi), self.owned['stat'])

    def test_both_remaining_user_controls_refuse(self):
        for path in (self.global_pause, self.texas):
            with self.subTest(path=path.name):
                path.write_text('user pause')
                with self.assertRaisesRegex(ValueError, 'blocks_admission'):
                    publisher.publication_controls(self.plan, self.registration)
                path.unlink()

    def test_unreviewed_result_and_rebound_review_refuse(self):
        for review in ({'accepted_mi_natural_drain_actual': False,
                        'result': self.plan['interim_drain']['evidence']},
                       {'accepted_mi_natural_drain_actual': True, 'result': {}}):
            with self.subTest(review=review):
                plan = copy.deepcopy(self.plan)
                plan['interim_drain']['review'] = h.save_new(
                    self.root, 'bad_review_' + str(len(list(self.root.iterdir()))), review)
                with self.assertRaisesRegex(ValueError, 'not_independently_accepted'):
                    publisher.publication_controls(plan, self.registration)

    def test_wrong_owner_or_routing_refuses(self):
        for field, value in [('process', {'pid': 43}), ('controller_id', 'c2')]:
            registration = copy.deepcopy(self.registration)
            registration['owners']['old_mi'][field] = value
            with self.assertRaisesRegex(ValueError, 'owner_or_marker_changed'):
                publisher.publication_controls(self.plan, registration)
        registration = copy.deepcopy(self.registration)
        registration['owners']['new_texas']['controller_fields']['pause_file'] = '/other'
        with self.assertRaisesRegex(ValueError, 'routing_changed'):
            publisher.publication_controls(self.plan, registration)

    def test_equal_bytes_replacement_is_not_owned_marker(self):
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.mi.read_bytes())
        replacement.replace(self.mi)
        with self.assertRaisesRegex(ValueError, 'marker_identity_changed'):
            publisher.publication_controls(self.plan, self.registration)

    def test_legacy_mode_keeps_all_pauses_effective(self):
        plan = copy.deepcopy(self.plan)
        plan['schema'] = 'causes.storage_phase_publication_plan.v1'
        with self.assertRaisesRegex(ValueError, 'explicit_interim'):
            publisher.publication_controls(plan, self.registration)
        del plan['interim_drain']
        with self.assertRaisesRegex(ValueError, 'blocks_admission'):
            publisher.publication_controls(plan, self.registration)

    def test_interim_order_is_only_initial_or_drain_to_retired(self):
        draining, retired = 'texas_running_mi_draining', 'texas_running_mi_retired'
        for phase, previous in [(draining, None), (retired, None), (retired, draining)]:
            publisher.validate_phase_order(phase, previous, interim=True)
        for phase, previous in [(draining, draining), (draining, retired),
                                (retired, retired), ('mi_waiting', None),
                                (retired, 'texas_waiting_mi_retired')]:
            with self.assertRaisesRegex(ValueError, 'order_changed'):
                publisher.validate_phase_order(phase, previous, interim=True)
        with self.assertRaisesRegex(ValueError, 'order_changed'):
            publisher.validate_phase_order(retired, None)
        publisher.validate_phase_order(retired, 'texas_waiting_mi_retired')

    def test_retired_phase_needs_actual_retired_owner_binding(self):
        registration = copy.deepcopy(self.registration)
        registration['phase'] = 'texas_running_mi_retired'
        with self.assertRaisesRegex(ValueError, 'owner_state_changed'):
            publisher.publication_controls(self.plan, registration)
        registration['owners']['old_mi']['observation_state'] = 'retired'
        publisher.publication_controls(self.plan, registration)

    def test_only_explicit_metadata_parent_is_created(self):
        pointer = self.root / 'controls' / 'next' / 'CURRENT.json'
        with self.assertRaisesRegex(ValueError, 'creation_not_authorized'):
            publisher.ensure_pointer_parent(self.plan, pointer, self.root)
        plan = {**self.plan, 'create_pointer_parent': True}
        publisher.ensure_pointer_parent(plan, pointer, self.root)
        self.assertEqual(list(pointer.parent.iterdir()), [])
        with self.assertRaisesRegex(ValueError, 'creation_not_authorized'):
            publisher.ensure_pointer_parent(plan, self.root / 'unrelated' / 'CURRENT.json', self.root)
        legacy = {**plan, 'schema': 'causes.storage_phase_publication_plan.v1'}
        with self.assertRaisesRegex(ValueError, 'creation_not_authorized'):
            publisher.ensure_pointer_parent(legacy, self.root / 'controls' / 'legacy' / 'CURRENT.json', self.root)

    def test_user_pause_racing_commit_preserves_pointer_absence(self):
        now = dt.datetime.now(dt.timezone.utc)
        reference = h.save_new(self.root, 'publish_authority',
                              {'root_authorized': True, 'no_user_revocation': True,
                               'action': publisher.ACTION, 'issued_at': now.isoformat(),
                               'expires_at': (now + dt.timedelta(seconds=120)).isoformat()})
        validate = lambda value: bool(publisher.publication_controls(self.plan, self.registration))
        authority = h.Authority(reference, publisher.ACTION,
                                publisher.publication_controls(self.plan, self.registration), validate)
        authority.check()
        pointer, records = self.root / 'CURRENT.json', self.root / 'publication'
        records.mkdir()
        def pause_before_commit():
            self.global_pause.write_text('user pause')
            authority.check()
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            publisher.atomic_pointer(pointer, {'phase': 'draining'}, None,
                                     records, pause_before_commit)
        self.assertFalse(pointer.exists())
        self.assertTrue((records / 'NEXT.pointer.json').exists())
        self.assertEqual(h.pin(self.mi), self.owned['pin'])


if __name__ == '__main__':
    unittest.main()
