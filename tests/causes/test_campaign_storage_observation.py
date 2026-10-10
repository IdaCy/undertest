import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes.campaign_storage_observation import observe_owner, observe_phase, validate_owner_identity
from causes.campaign_dispatcher import _SUCCESSION_PREDECESSOR


class StorageObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'output'
        self.meta = self.root / '.dispatcher'
        self.controller = self.meta / 'controllers/owner'
        self.controller.mkdir(parents=True)
        self.lock = (self.root / '.dispatcher.lock').open('xb')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(self.lock.close)
        process = {'pid': os.getpid(), 'process_starttime_ticks':
                   Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
                   'argv': [v.decode() for v in Path('/proc/self/cmdline').read_bytes().split(b'\0') if v]}
        self.queue = {'jobs_sha256': 'fixture'}
        fields = {**self.queue, 'workers': 2, 'max_full': 1,
                  'start_barrier_file': str(self.base / 'START_BARRIER')}
        self.queue_pin = h.save_new(self.meta, 'queue_identity.json', self.queue)
        identity = h.save_new(self.controller, 'identity.json', fields)
        self.start = {'event': 'controller_started', 'controller_id': 'owner', **self.queue}
        self.journal = self.meta / 'journal.jsonl'
        self.set_events([self.start])
        self.expected = {'observation_state': 'draining', 'process': process,
                         'controller_id': 'owner', 'output_root': str(self.root),
                         'output_lock': h.stat_identity(self.root / '.dispatcher.lock'),
                         'queue_identity': self.queue, 'queue_identity_pin': self.queue_pin,
                         'controller_identity_pin': identity, 'controller_fields': fields,
                         'journal_prefix': h.pin(self.journal)}

    def set_events(self, events):
        self.journal.write_bytes(b''.join(h.encoded(e) + b'\n' for e in events))

    def event(self, kind, **kw):
        return {'controller_id': 'owner', 'event': kind, **kw}

    def pause(self):
        return self.event('pause_seen', admission_stopped=True)

    def running(self):
        self.expected.update(observation_state='running', barrier_path=str(self.base / 'START_BARRIER'),
                             guard={'binding': {'output': str(self.root)},
                                    'directory': str(self.base / 'guard'), 'reporting_lock': {}})

    def waiting(self):
        self.running()
        marker = h.save_new(self.base, 'START_BARRIER', {'fixture': True})
        archived = h.save_new(self.base, 'ARCHIVED_MARKER', {'fixture': True})
        self.expected.update(observation_state='waiting', initial_pending=4,
                             owned_barrier=h.save_new(self.base, 'OWNERSHIP.json',
                                {'marker': marker, 'archived_marker': archived,
                                 'stat': h.stat_identity(Path(marker['path']))}))
        self.set_events([self.start, self.event('admission_barrier_waiting', active=0, pending=4)])

    def test_fresh_draining_owner_uses_terminal_chronology_and_preserved_prefix(self):
        self.set_events([self.start, self.event('claimed', run_id='a'),
                         self.event('claimed', run_id='b'), self.pause(),
                         self.event('unresolved_current_attempt', run_id='a')])
        value = observe_owner(self.expected)
        self.assertTrue(value['live'])
        self.assertEqual(value['active_claims'], 1)
        self.assertEqual(value['metadata_pins']['journal'], h.pin(self.journal))
        self.assertEqual(value['process_before'], value['process_after'])

    def approved_succession(self):
        old = {'dispatcher_sha256': _SUCCESSION_PREDECESSOR,
               'campaign_runner.py_sha256': 'same_runner', 'campaign_sandbox.py_sha256': 'same_sandbox'}
        new = {**old, 'dispatcher_sha256': 'reviewed_successor'}
        queue = {**self.queue, 'execution_code': old}
        queue_path = self.meta / 'queue_identity.json'
        queue_path.write_bytes(h.encoded(queue) + b'\n')
        identity = {**self.expected['controller_fields'], 'execution_code': new}
        identity_path = self.controller / 'identity.json'
        identity_path.write_bytes(h.encoded(identity) + b'\n')
        holds = h.save_new(self.base, 'HOLDS.json', {'held_ids': ['retained']})
        review = h.save_new(self.base, 'REVIEW.json', {'accepted_source_and_hold_membership': True,
                    'holds': holds, 'dispatcher': {'sha256': new['dispatcher_sha256']}})
        receipt = {'schema': 'caus.dispatcher_code_succession.v1', 'output_root': str(self.root),
                   'predecessor_queue_identity': h.pin(queue_path), 'successor_execution_code': new,
                   'admission_holds': holds, 'independent_review': review}
        ref = h.save_new(self.base, 'SUCCESSION.json', receipt)
        saved = self.meta / 'code_successions'
        saved.mkdir()
        h.save_new(saved, ref['sha256'] + '.json', receipt)
        start = {**self.start, 'execution_code': new}
        bound = self.event('admission_holds_bound', code_succession=ref,
                           admission_holds=holds, predecessor_queue_identity_retained=True)
        events = [start, bound, self.pause()]
        self.set_events(events)
        self.expected.update(queue_identity=queue, queue_identity_pin=h.pin(queue_path),
                             controller_fields=identity, controller_identity_pin=h.pin(identity_path),
                             code_succession=ref, journal_prefix=h.pin(self.journal))
        return queue, identity, start, events

    def test_explicit_retained_code_succession_observes_original_queue(self):
        queue, identity, start, events = self.approved_succession()
        value = observe_owner(self.expected)
        self.assertEqual(value['queue_identity'], queue)
        self.assertEqual(value['metadata_pins']['code_succession'], self.expected['code_succession'])
        self.assertEqual(value['active_claims'], 0)

    def test_succession_rejects_unbound_running_scientific_and_other_code_changes(self):
        queue, identity, start, events = self.approved_succession()
        for change in ('unbound', 'running', 'jobs', 'runner', 'start'):
            with self.subTest(change=change):
                expected, current, began = copy.deepcopy(self.expected), copy.deepcopy(identity), copy.deepcopy(start)
                if change == 'unbound':
                    expected.pop('code_succession')
                elif change == 'running':
                    expected['observation_state'] = 'running'
                elif change == 'jobs':
                    current['jobs_sha256'] = 'changed'
                elif change == 'runner':
                    current['execution_code']['campaign_runner.py_sha256'] = 'changed'
                else:
                    began['execution_code']['dispatcher_sha256'] = 'changed'
                with self.assertRaises(ValueError):
                    validate_owner_identity(expected, queue, current, began, events)

    def test_succession_requires_saved_receipt_and_exact_owner_event(self):
        queue, identity, start, events = self.approved_succession()
        for bound in ([], [events[1], events[1]], [{**events[1], 'code_succession': None}]):
            with self.subTest(bound=bound):
                with self.assertRaisesRegex(ValueError, 'succession_event_changed'):
                    validate_owner_identity(self.expected, queue, identity, start, [start, *bound, self.pause()])
        saved = self.meta / 'code_successions' / (self.expected['code_succession']['sha256'] + '.json')
        saved.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'saved_code_succession_changed'):
            validate_owner_identity(self.expected, queue, identity, start, events)

    def owned_idle_retirement(self):
        self.expected['observation_state'] = 'retired'
        self.expected['controller_fields']['pause_file'] = str(self.base / 'PAUSE')
        identity_path = self.controller / 'identity.json'
        identity_path.write_bytes(h.encoded(self.expected['controller_fields']) + b'\n')
        self.expected['controller_identity_pin'] = h.pin(identity_path)
        prefix = [self.start, self.pause(), self.event('claimed', run_id='old'),
                  self.event('episode_finished', run_id='old')]
        self.set_events(prefix)
        self.expected['journal_prefix'] = h.pin(self.journal)
        preflight = h.save_new(self.base, 'PREFLIGHT.json', {
            'at': '2026-01-01T00:00:00+00:00', 'owners': {'old_mi': {
                'binding': copy.deepcopy(self.expected), 'active_claims': 0, 'children': [], 'claims': 1}}})
        plan, adoption = {'path': 'fixture_plan'}, {'path': 'fixture_adoption'}
        preparation = h.save_new(self.base, 'DRAIN_PREPARATION.json', {'plan': plan})
        review = h.save_new(self.base, 'DRAIN_REVIEW.json', {'accepted_staging_and_drain_preparation': True,
                                                          'preparation': preparation})
        authority = h.save_new(self.base, 'AUTHORITY.json', {'root_authorized': True,
            'no_user_revocation': True, 'action': 'storage_owned_idle_drain', 'plan': plan,
            'root_adoption': adoption, 'independent_review': review,
            'issued_at': '2026-01-01T00:00:00+00:00', 'expires_at': '2026-01-01T00:03:00+00:00'})
        marker_value = {'origin': 'storage_transition', 'transition_id': 'fixture',
                        'at': '2026-01-01T00:00:01+00:00', 'authority': authority,
                        'plan': plan, 'root_adoption': adoption}
        marker = h.save_new(self.base, 'PAUSE', marker_value)
        archive = h.save_new(self.base, 'PAUSE_ARCHIVE.json', marker_value)
        ownership = h.save_new(self.base, 'OWNED_MARKER.json', {'origin': 'storage_transition',
            'transition_id': 'fixture', 'pin': marker, 'archived_marker': archive,
            'stat': h.stat_identity(Path(marker['path']))})
        self.expected['owned_retirement'] = {'preflight': preflight, 'owned_marker': ownership, 'role': 'old_mi'}
        suffix = [self.event('controller_paused', at='2026-01-01T00:00:02+00:00', pending=2)]
        self.set_events(prefix + suffix)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        return prefix, suffix

    def test_owned_retirement_preserves_lifetime_pause_history(self):
        self.owned_idle_retirement()
        with patch('causes.campaign_storage_observation.h.retirement_state',
                   return_value={'pid': os.getpid(), 'absent': True}):
            value = observe_owner(self.expected)
            self.assertEqual(value['drain_progress']['acknowledgement_event'], 'controller_paused')
            self.assertEqual(value['claims'], 1)
            self.assertEqual(value['active_claims'], 0)
            ordinary = copy.deepcopy(self.expected)
            ordinary.pop('owned_retirement')
            with self.assertRaisesRegex(ValueError, 'claim_after_pause_acknowledgement'):
                observe_owner(ordinary)

    def test_owned_retirement_rejects_new_claim_other_owner_or_missing_ack(self):
        prefix, suffix = self.owned_idle_retirement()
        changes = [[], [*suffix, self.event('claimed', run_id='new')],
                   [{**suffix[0], 'controller_id': 'other'}], [suffix[0], suffix[0]],
                   [{**suffix[0], 'at': '2025-12-31T00:00:00+00:00'}]]
        with patch('causes.campaign_storage_observation.h.retirement_state',
                   return_value={'pid': os.getpid(), 'absent': True}):
            for changed in changes:
                with self.subTest(changed=changed):
                    self.set_events(prefix + changed)
                    with self.assertRaises(ValueError):
                        observe_owner(self.expected)
            self.set_events(prefix + suffix)
            marker = self.base / 'PAUSE'
            marker.write_bytes(marker.read_bytes())
            with self.assertRaisesRegex(ValueError, 'marker_changed'):
                observe_owner(self.expected)

    def test_terminal_before_claim_and_claim_after_pause_fail(self):
        for middle in ([self.event('episode_finished', run_id='a'),
                        self.event('claimed', run_id='a'), self.pause()],
                       [self.pause(), self.event('claimed', run_id='a')]):
            with self.subTest(middle=middle):
                self.set_events([self.start, *middle])
                with self.assertRaises(ValueError):
                    observe_owner(self.expected)

    def test_replaced_lock_changed_prefix_and_torn_journal_fail(self):
        self.set_events([self.start, self.pause()])
        wrong = copy.deepcopy(self.expected)
        wrong['output_lock']['st_ino'] += 1
        with self.assertRaisesRegex(ValueError, 'lock_identity_changed'):
            observe_owner(wrong)
        wrong = copy.deepcopy(self.expected)
        wrong['journal_prefix']['sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'journal_prefix_changed'):
            observe_owner(wrong)
        self.journal.write_bytes(self.journal.read_bytes().rstrip(b'\n'))
        with self.assertRaisesRegex(ValueError, 'journal_missing_torn'):
            observe_owner(self.expected)

    def test_retired_requires_actual_absence_free_lock_and_no_active_claims(self):
        self.expected['observation_state'] = 'retired'
        self.set_events([self.start, self.pause()])
        with self.assertRaisesRegex(ValueError, 'not_retired'):
            observe_owner(self.expected)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        with patch('causes.campaign_storage_observation.h.retirement_state',
                   return_value={'pid': os.getpid(), 'absent': True}):
            self.assertFalse(observe_owner(self.expected)['live'])
            self.set_events([self.start, self.event('claimed', run_id='lost'), self.pause()])
            with self.assertRaisesRegex(ValueError, 'unresolved_claim'):
                observe_owner(self.expected)

    def test_waiting_accepts_fixed_pending_queue_but_never_work_or_bad_guard(self):
        self.waiting()
        with patch('causes.campaign_storage_observation.observe_guard', return_value={'healthy': True}):
            self.assertEqual(observe_owner(self.expected)['claims'], 0)
            (self.meta / 'blocks').mkdir()
            (self.meta / 'blocks/block.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'created_block'):
                observe_owner(self.expected)
        # An actual guard failure must not become a healthy phase observation.
        self.running()
        Path(self.expected['barrier_path']).rename(self.base / 'RETAINED_BARRIER')
        with patch('causes.campaign_storage_observation.observe_guard', return_value={'healthy': False}):
            with self.assertRaisesRegex(ValueError, 'guard_observation_unhealthy'):
                observe_owner(self.expected)

    def test_running_rejects_missing_lock_or_reappearing_barrier(self):
        self.running()
        with patch('causes.campaign_storage_observation.observe_guard', return_value={'healthy': True}):
            self.assertFalse(observe_owner(self.expected)['barrier_present'])
            Path(self.expected['barrier_path']).write_text('pause')
            with self.assertRaisesRegex(ValueError, 'still_barred'):
                observe_owner(self.expected)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        with self.assertRaisesRegex(ValueError, 'lock_owner_changed'):
            observe_owner(self.expected)

    def test_unrelated_barrier_cannot_replace_actual_controller_barrier(self):
        self.waiting()
        self.expected['barrier_path'] = str(self.base / 'UNRELATED_BARRIER')
        with self.assertRaisesRegex(ValueError, 'barrier_path_changed'):
            observe_owner(self.expected)
        self.expected['observation_state'] = 'running'
        with self.assertRaisesRegex(ValueError, 'barrier_path_changed'):
            observe_owner(self.expected)

    def test_phase_cannot_substitute_wrong_requested_state(self):
        self.set_events([self.start, self.pause()])
        registration = {'schema': 'causes.storage_controller_phase.v1', 'phase': 'mi_waiting',
                        'owners': {'old_mi': self.expected}}
        with self.assertRaisesRegex(ValueError, 'exact_storage_phase_owners'):
            observe_phase(registration)

    def reporter_receipt(self):
        self.running()
        source_path = self.base / 'retained_storage.py'
        source_path.write_text('# retained reporter source fixture\n')
        source = h.pin(source_path)
        guard = self.expected['guard']
        guard['reporter_source'] = source
        receipt = {'process': self.expected['process'],
                   'controller_id': self.expected['controller_id'],
                   'binding': {'output': str(self.root), 'storage_binding': guard['binding'],
                               'storage_source_sha256': source['sha256']},
                   'guard_directory': guard['directory'],
                   'guard_reporting_lock': guard['reporting_lock']}
        self.expected['launch_owner_receipt'] = h.save_new(self.base, 'OWNER.json', receipt)
        return receipt, source

    def test_reporter_source_requires_exact_launch_attestation(self):
        receipt, source = self.reporter_receipt()
        with patch('causes.campaign_storage_observation.observe_guard',
                   return_value={'healthy': True}) as guard:
            value = observe_owner(self.expected)
            self.assertEqual(guard.call_args.kwargs, {'reporter_source': source})
            self.assertEqual(value['metadata_pins']['guard_reporter_source'], source)
            self.assertEqual(value['metadata_pins']['launch_owner_receipt'],
                             self.expected['launch_owner_receipt'])
        absent = copy.deepcopy(self.expected)
        absent.pop('launch_owner_receipt')
        with self.assertRaisesRegex(ValueError, 'reporter_receipt_required'):
            observe_owner(absent)
        absent = copy.deepcopy(self.expected)
        absent['guard']['reporter_source'] = None
        with self.assertRaisesRegex(ValueError, 'reporter_receipt_required'):
            observe_owner(absent)

    def test_reporter_attestation_rejects_other_owner_and_guard_bindings(self):
        receipt, source = self.reporter_receipt()
        changes = [('pid', lambda r: r['process'].update(pid=1)),
                   ('start', lambda r: r['process'].update(process_starttime_ticks='wrong')),
                   ('argv', lambda r: r['process'].update(argv=['other'])),
                   ('controller', lambda r: r.update(controller_id='other')),
                   ('output', lambda r: r['binding'].update(output='/other')),
                   ('binding', lambda r: r['binding']['storage_binding'].update(extra=True)),
                   ('source', lambda r: r['binding'].update(storage_source_sha256='wrong')),
                   ('directory', lambda r: r.update(guard_directory='/other')),
                   ('lock', lambda r: r.update(guard_reporting_lock={'wrong': True}))]
        for name, change in changes:
            with self.subTest(field=name):
                wrong = copy.deepcopy(receipt)
                change(wrong)
                expected = copy.deepcopy(self.expected)
                expected['launch_owner_receipt'] = h.save_new(self.base, name + '.json', wrong)
                with self.assertRaisesRegex(ValueError, 'reporter_receipt_changed'):
                    observe_owner(expected)

    def test_reporter_source_and_receipt_tampering_refuse_before_or_after_sample(self):
        receipt, source = self.reporter_receipt()
        source_path = Path(source['path'])
        original = source_path.read_bytes()
        source_path.write_bytes(original + b'# tampered\n')
        with self.assertRaisesRegex(ValueError, 'input_pin_changed'):
            observe_owner(self.expected)
        source_path.write_bytes(original)
        receipt_path = Path(self.expected['launch_owner_receipt']['path'])
        def mutate_receipt(*args, **kwargs):
            receipt_path.write_text('{}')
            return {'healthy': True}
        with patch('causes.campaign_storage_observation.observe_guard', side_effect=mutate_receipt):
            with self.assertRaisesRegex(ValueError, 'input_pin_changed'):
                observe_owner(self.expected)


if __name__ == '__main__':
    unittest.main()
