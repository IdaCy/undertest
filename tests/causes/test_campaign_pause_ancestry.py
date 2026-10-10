import copy
import os
from pathlib import Path
import unittest

from causes import campaign_handoff as h
from causes import campaign_storage as storage
import test_campaign_storage_controls as controls


class PauseAncestryTests(unittest.TestCase):
    def setUp(self):
        self.case = controls.StorageControlGuardTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.serial = 0
        self.binding = copy.deepcopy(self.case.binding)

    def saved(self, value):
        self.serial += 1
        return h.save_new(self.case.base, f'ancestry_{self.serial}.json', value)

    def advance(self):
        control = self.binding['admission_control']
        prior_ref = control['pause_routing']
        prior = h.checked(prior_ref)
        marker_path = Path(prior['successor_local_pauses']['mi'])
        marker = h.save_new(marker_path.parent, marker_path.name, {'owned_drain': self.serial})
        ownership = self.saved({'schema': 'causes.retired_owned_pause.v1', 'role': 'mi',
            'owner_retired': True, 'marker': marker, 'stat': h.stat_identity(marker_path),
            'predecessor_routing': prior_ref})
        review = self.saved({'accepted_retired_owned_pause': True, 'ownership': ownership})
        routing = copy.deepcopy(prior)
        routing['schema'] = 'causes.storage_pause_routing.v2'
        routing['predecessor_routing'] = prior_ref
        routing['retained_owned_pauses'] = prior.get('retained_owned_pauses', []) + [
            {'evidence': ownership, 'review': review}]
        routing['successor_local_pauses']['mi'] = str(self.case.base / f'MI_PAUSE_NEXT_{self.serial}')
        routing['user_pause_fanout'] = prior['user_pause_fanout'] + [routing['successor_local_pauses']['mi']]
        control['pause_routing'] = self.saved(routing)
        return routing, marker_path

    def guard(self, role='mi'):
        binding = copy.deepcopy(self.binding)
        binding['admission_control']['role'] = role
        return storage.StorageGuard(binding, observe=lambda _: self.case.observed)

    def change_routing(self, routing):
        self.binding['admission_control']['pause_routing'] = self.saved(routing)

    def test_repeated_succession_preserves_every_drain_and_global_control(self):
        first, first_marker = self.advance()
        second, second_marker = self.advance()
        self.assertEqual(self.guard()(), 140)
        self.assertEqual(self.guard('texas')(), 140)
        self.assertTrue(first_marker.exists() and second_marker.exists() and self.case.old.exists())
        self.case.global_pause.write_text('user pause')
        self.assertEqual(self.guard()(), 0)
        self.assertEqual(self.guard('texas')(), 0)

    def test_user_edit_or_touch_of_retired_control_blocks_new_owner(self):
        _, marker = self.advance()
        checker = self.guard()
        self.assertEqual(checker(), 140)
        before = marker.stat()
        os.utime(marker, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(checker(), 0)
        self.assertIn('retained_owned_pause_changed', checker.last_error)
        marker.write_text('user pause')
        self.assertEqual(checker(), 0)
        self.assertIn('pin_changed', checker.last_error)

    def test_omitting_old_ancestry_cannot_silently_resume(self):
        self.advance()
        routing, _ = self.advance()
        routing['retained_owned_pauses'] = routing['retained_owned_pauses'][1:]
        self.change_routing(routing)
        checker = self.guard()
        self.assertEqual(checker(), 0)
        self.assertIn('complete_ordered_pause_ancestry', checker.last_error)

    def test_changing_inherited_global_or_texas_control_is_refused(self):
        routing, _ = self.advance()
        for field in ('global', 'texas'):
            bad = copy.deepcopy(routing)
            if field == 'global':
                bad['successor_global_pause'] = str(self.case.base / 'FOREIGN_GLOBAL')
            else:
                bad['successor_local_pauses']['texas'] = str(self.case.base / 'FOREIGN_TEXAS')
            self.change_routing(bad)
            with self.subTest(field=field):
                self.assertEqual(self.guard()(), 0)

    def test_missing_review_or_unretired_owner_cannot_qualify_marker(self):
        routing, _ = self.advance()
        pair = routing['retained_owned_pauses'][-1]
        for kind in ('review', 'retired'):
            bad = copy.deepcopy(routing)
            if kind == 'review':
                bad['retained_owned_pauses'][-1]['review'] = self.saved({
                    'accepted_retired_owned_pause': False, 'ownership': pair['evidence']})
            else:
                value = h.checked(pair['evidence']); value['owner_retired'] = False
                evidence = self.saved(value)
                bad['retained_owned_pauses'][-1] = {'evidence': evidence,
                    'review': self.saved({'accepted_retired_owned_pause': True, 'ownership': evidence})}
            self.change_routing(bad)
            with self.subTest(kind=kind):
                self.assertEqual(self.guard()(), 0)

    def test_legacy_schema_cannot_hide_extra_pauses(self):
        routing, _ = self.advance()
        routing['schema'] = 'causes.storage_pause_routing.v1'
        self.change_routing(routing)
        checker = self.guard()
        self.assertEqual(checker(), 0)
        self.assertIn('pause_ancestry_requires_explicit_schema', checker.last_error)

    def test_user_pause_at_new_local_path_is_still_effective(self):
        routing, _ = self.advance()
        Path(routing['successor_local_pauses']['mi']).write_text('user pause')
        self.assertEqual(self.guard()(), 0)
        self.assertEqual(self.guard('texas')(), 140)


if __name__ == '__main__':
    unittest.main()
