import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from causes import campaign_capacity_trial as trial
from causes import campaign_handoff as h
from causes import campaign_storage as storage
from test_campaign_storage import storage_fixture


class PrefixAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0
        self.marker = self.root / 'PREDECESSOR_PAUSE'
        self.marker.write_text(json.dumps({'origin': 'reviewed_finite_prefix_handoff',
                                          'owner': 'original-owner'}))
        self.original_marker = self.marker.read_bytes()
        self.marker_row = {'pin': h.pin(self.marker), 'stat': h.stat_identity(self.marker)}
        remote = self.save('remote', {'owned_marker': self.marker_row})
        journal = self.root / 'journal.jsonl'
        journal.write_bytes(b'')
        self.closed = {'schema': 'causes.finite_prefix_predecessor_closure.v1',
                       'journal': h.pin(journal), 'remote_closure': remote,
                       'remote_owner_retired': True, 'local_transport_retired': True,
                       'all_claims_accounted': True, 'route_history_preserved': True,
                       'errors': []}
        closed = self.save('closed', self.closed)
        peer = self.save('closed-peer', {'accepted_finite_prefix_predecessor_closure': True,
                                        'actual': closed, 'journal': self.closed['journal']})
        self.predecessor = {'evidence': closed, 'review': peer}
        self.holds = [str(self.root / name) for name in
                      ('TRIAL_PAUSE', 'GLOBAL_PAUSE', 'INHERITED_PAUSE', 'GO_REVOKED')]
        self.barrier = self.root / 'ADMISSION_BARRIER'
        self.barrier.write_text('owned startup barrier')
        self.physical, self.observation = storage_fixture()
        self.value = {'schema': 'causes.capacity_operational_binding_addendum.v1',
                      'criteria': {'fixture': 'criteria'}, 'criteria_adoption': {'fixture': 'root'},
                      'candidate': {'output_namespace': self.physical['output'],
                                    'sole_hold_path': self.holds[0],
                                    'startup_barrier': str(self.barrier), 'workers': 8, 'max_full': 0,
                                    'admission_controls': {
                                        'predecessor': self.predecessor,
                                        'retained_markers': [self.marker_row],
                                        'operator_hold_paths': self.holds,
                                        'global_pause_file': self.holds[1]}}}
        self.bind()

    def save(self, name, value):
        self.serial += 1
        return h.save_new(self.root, f'{self.serial}-{name}.json', value)

    def bind(self):
        evidence = self.save('addendum', self.value)
        common = {key: self.value[key] for key in ('criteria', 'criteria_adoption')}
        peer = self.save('peer', dict(common, accepted_capacity_operational_binding_addendum=True,
                                      addendum=evidence))
        adoption = self.save('adoption', dict(common,
            root_adopted_capacity_operational_binding_addendum=True,
            addendum=evidence, independent_review=peer))
        self.refs = {'evidence': evidence, 'review': peer, 'adoption': adoption}
        self.control = {'schema': 'causes.finite_prefix_admission_controls.v1',
                        'role': 'mi', 'operational_bindings': self.refs}
        self.physical['admission_control'] = self.control
        self.binding = {'output_root': self.physical['output'], 'pause_file': self.holds[0],
                        'owned_startup_barrier': {'fixture': 'owned'},
                        'criteria': {'evidence': self.value['criteria']},
                        'criteria_adoption': self.value['criteria_adoption']}
        self.owned = {'marker': {'path': str(self.barrier)}}
        self.policy = {'launch': {'storage_binding': self.physical,
                                 'owned_barrier': self.binding['owned_startup_barrier'],
                                 'options': {'pause_file': self.holds[0],
                                             'global_pause_file': self.holds[1]}},
                       'capacity_trial': {'operational_bindings': self.refs, 'mode': 'candidate'}}
        self.plan = {'profile': 'finite_prefix', 'operational_bindings': self.refs,
                     'sampling': {'guard': {'binding': self.physical},
                                  'controls_absent': list(self.holds)},
                     'assignment': {'evidence': self.save('assignment',
                                                         {'predecessors': [self.predecessor]})}}

    def guard(self):
        return storage.StorageGuard(self.physical, observe=lambda _: self.observation)

    def test_existing_adoption_and_nonstorage_marker_are_preserved(self):
        trial.validate_operational_bindings(self.plan, self.policy, self.binding, self.owned)
        trial.validate_trial_controls(self.plan, self.policy, self.binding, self.owned)
        observed = storage.storage_control_observation(self.control)
        self.assertEqual(observed['operational_bindings'], self.refs)
        self.assertEqual(observed['predecessor'], self.predecessor)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)
        self.assertNotIn(str(self.barrier), observed['controls_present'])
        self.assertTrue(self.barrier.exists())
        self.assertGreater(self.guard()(), 0)

    def test_every_inherited_operator_hold_blocks_each_native_guard_check(self):
        guard = self.guard()
        for path in self.holds:
            with self.subTest(path=path):
                self.assertGreater(guard(), 0)
                Path(path).write_text('operator hold')
                self.assertEqual(guard(), 0)
                self.assertIn('operator_hold_blocks_admission', guard.last_error)
                Path(path).unlink()
                self.assertGreater(guard(), 0)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_marker_byte_change_fails_closed(self):
        guard = self.guard()
        self.marker.write_text('replacement marker')
        self.assertEqual(guard(), 0)
        self.assertIn('pin_changed', guard.last_error)

    def test_marker_stat_change_fails_closed_without_byte_change(self):
        before = self.marker.stat()
        os.utime(self.marker, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        guard = self.guard()
        self.assertEqual(guard(), 0)
        self.assertIn('retained_marker_changed', guard.last_error)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_unretired_predecessor_is_rejected(self):
        self.closed['local_transport_retired'] = False
        closed = self.save('open-closed', self.closed)
        self.predecessor = {'evidence': closed, 'review': self.save('open-peer', {
            'accepted_finite_prefix_predecessor_closure': True,
            'actual': closed, 'journal': self.closed['journal']})}
        self.value['candidate']['admission_controls']['predecessor'] = self.predecessor
        self.bind()
        self.assertEqual(self.guard()(), 0)

    def test_wrong_closed_owner_marker_is_rejected(self):
        other = self.root / 'OTHER_PAUSE'
        other.write_text('another owner')
        self.value['candidate']['admission_controls']['retained_markers'] = [
            {'pin': h.pin(other), 'stat': h.stat_identity(other)}]
        self.bind()
        with self.assertRaisesRegex(ValueError, 'closed_owner_marker_missing'):
            storage.storage_control_observation(self.control)

    def test_assignment_must_use_the_same_accepted_predecessor(self):
        self.plan['assignment']['evidence'] = self.save('foreign-assignment', {'predecessors': []})
        with self.assertRaisesRegex(ValueError, 'candidate_control_scope_changed'):
            trial.validate_trial_controls(self.plan, self.policy, self.binding, self.owned)

    def test_sampler_cannot_drop_an_inherited_hold(self):
        self.plan['sampling']['controls_absent'].pop()
        with self.assertRaisesRegex(ValueError, 'candidate_control_scope_changed'):
            trial.validate_trial_controls(self.plan, self.policy, self.binding, self.owned)

    def test_startup_barrier_cannot_be_an_operator_hold(self):
        self.value['candidate']['admission_controls']['operator_hold_paths'].append(str(self.barrier))
        self.bind()
        with self.assertRaisesRegex(ValueError, 'controls_overlap'):
            storage.storage_control_observation(self.control)

    def test_wrong_profile_or_operational_chain_is_rejected(self):
        for key, value in (('profile', 'mixed'), ('operational_bindings', {})):
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan)
                plan[key] = value
                with self.assertRaisesRegex(ValueError, 'control_profile_or_binding_changed'):
                    trial.validate_trial_controls(plan, self.policy, self.binding, self.owned)

    def test_control_binding_cannot_move_to_another_output(self):
        self.physical['output'] += '-other'
        guard = self.guard()
        self.assertEqual(guard(), 0)
        self.assertIn('control_output_changed', guard.last_error)

    def test_root_adoption_must_join_the_reviewed_addendum(self):
        self.refs['adoption'] = self.save('wrong-adoption', {
            'root_adopted_capacity_operational_binding_addendum': True,
            'addendum': {'wrong': 'evidence'}, 'independent_review': self.refs['review']})
        with self.assertRaisesRegex(ValueError, 'operational_binding_unaccepted'):
            storage.storage_control_observation(self.control)


if __name__ == '__main__':
    unittest.main()
