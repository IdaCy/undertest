import copy
import hashlib
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from causes import campaign_supplement_closed_audit as audit


class ClosedAuditTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(mode='completed_actor_direct_probe', maximum_new_requests=2079,
                         initial_failures=0, capacity_limit={'pin': 'capacity'},
                         local_free_space_floor_bytes=1234)
        self.start = dict(event='controller_started', phase=self.plan['mode'], existing_finals=0,
                          pending=2079, inherited_failures=0, max_clients=8,
                          max_new_requests=2079, queue_prefix_count=2079,
                          capacity_limit=self.plan['capacity_limit'], local_free_space_floor_bytes=1234)

    def summary(self, rows, *, counter=0, stop=None, claimed=2, completed=None):
        if completed is None:
            completed = ['a', 'b']
        result = dict(phase=self.plan['mode'], existing_final=0, active=0, stop_reason=stop,
                      consecutive_failures=counter, completed_ids=completed,
                      new_final=len(completed), remaining_unclaimed=2079-claimed,
                      local_free_space_floor_bytes=1234)
        events = [self.start]+rows+[dict(event='controller_finished', **result)]
        return audit.native_summary(events, result, self.plan, stop)

    def rows(self):
        return [dict(event='claimed', measurement_id=mid) for mid in ('a', 'b')]+[
            dict(event='route_success', measurement_id=mid, consecutive_failures=0) for mid in ('b', 'a')]

    def test_large_scope_preserves_native_completion_order(self):
        result = self.summary(self.rows(), completed=['b', 'a'])
        self.assertEqual(result['consecutive_failures'], 0)
        self.assertEqual(result['unresolved_claimed_ids'], [])

    def test_wrong_completion_order_refuses(self):
        with self.assertRaisesRegex(ValueError, 'native_final_counters_and_order'):
            self.summary(self.rows())

    def test_wrong_old_request_ceiling_refuses(self):
        self.start['pending'] = 64
        with self.assertRaisesRegex(ValueError, 'native_controller_start'):
            self.summary(self.rows(), completed=['b', 'a'])

    def test_failure_counter_cannot_be_reset(self):
        rows = self.rows(); rows[-1] = dict(event='route_failure', measurement_id='a', consecutive_failures=1)
        self.assertEqual(self.summary(rows, counter=1, completed=['b', 'a'])['technical_failures'], 1)
        with self.assertRaisesRegex(ValueError, 'native_final_counters_and_order'):
            self.summary(rows, completed=['b', 'a'])

    def test_inherited_counter_and_quarantine_are_retained(self):
        self.plan['initial_failures'] = self.start['inherited_failures'] = 2
        rows = [dict(event='claimed', measurement_id='a'),
                dict(event='route_failure', measurement_id='a', consecutive_failures=3),
                dict(event='route_quarantined', reason='three_consecutive_infrastructure_failures')]
        result = self.summary(rows, counter=3, stop='three_consecutive_infrastructure_failures', claimed=1, completed=['a'])
        self.assertTrue(result['quarantined'])
        self.assertEqual(result['consecutive_failures'], 3)

    def test_unresolved_claim_and_operational_hold_stay_distinct(self):
        hold = dict(event='admission_held', reason='operator_pause')
        rows = [dict(event='claimed', measurement_id='a'), hold]
        result = self.summary(rows, stop='operator_pause', claimed=1, completed=[])
        self.assertEqual(result['unresolved_claimed_ids'], ['a'])
        self.assertEqual(result['events'], [hold])
        self.assertFalse(result['quarantined'])

    def test_capacity_binding_cannot_change(self):
        self.start['capacity_limit'] = {'pin': 'other'}
        with self.assertRaisesRegex(ValueError, 'native_controller_start'):
            self.summary(self.rows(), completed=['b', 'a'])

    def test_plain_buffers_exact_native_bytes_without_writes(self):
        outputs, sizes = {}, []
        body = b'[1, 2, 3]'
        with patch.object(Path, 'open', side_effect=AssertionError('write forbidden')):
            ref = audit.buffered_evidence('FULL_JOBS.v1.json', body, Path('/memory'), outputs, None, sizes,
                                           raw_limit=100, plain_limit=100)
        self.assertEqual(outputs[Path(ref['path'])], body)
        self.assertEqual(ref['sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(sizes[0]['encoding'], 'identity')

    def test_raw_and_plain_bounds_refuse_before_buffer(self):
        for raw_limit, plain_limit in ((2, 4), (4, 2), (True, 4)):
            outputs = {}
            with self.assertRaises(ValueError):
                audit.buffered_evidence('FULL_JOBS.v1.json', b'123', Path('/memory'), outputs, None, [],
                                        raw_limit=raw_limit, plain_limit=plain_limit)
            self.assertEqual(outputs, {})

    def test_duplicate_output_refuses(self):
        outputs = {Path('/memory/FULL_JOBS.v1.json'): b'old'}
        with self.assertRaisesRegex(ValueError, 'duplicate_buffered_output'):
            audit.buffered_evidence('FULL_JOBS.v1.json', b'new', Path('/memory'), outputs, None, [])
        self.assertEqual(outputs[Path('/memory/FULL_JOBS.v1.json')], b'old')

    def test_opened_and_named_lock_identity_both_matter(self):
        descriptor = dict(path='/memory/lock', device=1, inode=2)
        stream = SimpleNamespace(fileno=lambda: 7)
        good = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=2)
        other = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=3)
        with patch.object(audit.os, 'fstat', return_value=good), patch.object(Path, 'stat', return_value=good):
            audit.lock_identity(stream, descriptor)
            with patch.object(Path, 'stat', return_value=other):
                with self.assertRaisesRegex(ValueError, 'held_lock_identity'):
                    audit.lock_identity(stream, descriptor)


if __name__ == '__main__':
    unittest.main()
