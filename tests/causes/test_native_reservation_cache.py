"""Synthetic end-to-end evidence checks for immutable reservation proof reuse."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_admission as a
from causes import campaign_census as c
from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from tests.causes import test_native_reservation_growth as fixture_module


def fixture(test, *, expanded=False, waiting=False):
    value = fixture_module.NativeReservationGrowthTests()
    value.setUp()
    test.addCleanup(value.doCleanups)
    value.build(expanded=expanded)
    if waiting:
        value.refresh(waiting=True)
    return value


def operator(value):
    admission, foundation = value.operator(value.old, value.current, value.allocation, value.config_ref)
    admission.policy.update(reservation_transition='exact_own_and_foreign_blocks_v1',
                            reservation_growth=value.growth, census_hosts=['fixture', 'other'])
    peer = h.checked(admission.inputs['fresh_reservation']['review'])
    peer.update(accepted_dual_host_capture_custody=True, accepted_native_foreign_block_extension=True,
                generation_reservation=admission.inputs['precreation_reservation']['evidence'],
                reservation_growth=value.growth, capture_pins=value.current['capture_pins'])
    admission.inputs['fresh_reservation']['review'] = value.save(peer)
    return admission, foundation


def check(value, cache=None, *, waiting=False):
    return a.native_reservation_foreign_blocks(value.old, value.current, value.allocation,
        value.growth, value.config_ref, waiting=waiting, proof_cache=cache)


class ReservationReuseTests(unittest.TestCase):
    def test_all_514_jobs_and_unclaimed_foreign_rows_match_uncached_proof(self):
        value = fixture(self, expanded=True)
        before = copy.deepcopy((value.old, value.current, value.allocation, value.growth))
        self.assertEqual((len(value.jobs), sum(row['allocation']['continue_to_end'] for row in value.jobs)), (514, 2))
        self.assertTrue(check(value))
        cache = a.NativeReservationProofCache()
        with patch.object(c, 'compose_reservation', wraps=c.compose_reservation) as compose:
            for _ in range(3):
                self.assertTrue(check(value, cache))
            self.assertEqual(compose.call_count, 2)
        self.assertEqual((value.old, value.current, value.allocation, value.growth), before)
        self.assertFalse(Path(value.allocation['output_root']).exists())

    def test_uncached_calls_and_separate_cache_instances_reconstruct_independently(self):
        value = fixture(self)
        with patch.object(c, 'compose_reservation', wraps=c.compose_reservation) as compose:
            check(value); check(value)
            check(value, a.NativeReservationProofCache())
            check(value, a.NativeReservationProofCache())
        self.assertEqual(compose.call_count, 8)

    def test_six_complete_evidence_calls_repeat_acceptance_freshness_and_donors(self):
        value = fixture(self)
        admission, foundation = operator(value)
        with patch.object(a, '_native_reservation_foreign_blocks_proof',
                          wraps=a._native_reservation_foreign_blocks_proof) as proof, \
             patch.object(a, 'accepted', wraps=a.accepted) as acceptance, \
             patch.object(a, 'recent', wraps=a.recent) as freshness, \
             patch.object(a, 'donor_cuts_clear', wraps=a.donor_cuts_clear) as donors:
            for _ in range(6):
                self.assertEqual(admission.evidence(admission.start_action), foundation)
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(acceptance.call_count, 18)
            self.assertEqual(freshness.call_count, 30)  # both ends of two hosts + foundation
            self.assertEqual(donors.call_count, 6)

    def test_warm_proof_cannot_hide_changed_independent_review_or_foundation(self):
        for target in ('fresh_reservation', 'foundation'):
            with self.subTest(target=target):
                value = fixture(self)
                admission, _ = operator(value)
                admission.evidence(admission.start_action)
                ref = admission.inputs[target]['review' if target == 'fresh_reservation' else 'evidence']
                path = Path(ref['path']); path.write_bytes(path.read_bytes() + b' ')
                with self.assertRaisesRegex(ValueError, 'handoff_input_pin_changed'):
                    admission.evidence(admission.start_action)

    def test_same_call_object_reuse_keeps_original_second_byte_check(self):
        value = fixture(self)
        admission, _ = operator(value)
        original = a.accepted
        def accept(pair, flag, key):
            result = original(pair, flag, key)
            if pair == admission.inputs['foundation']:
                path = Path(admission.inputs['precreation_reservation']['evidence']['path'])
                path.write_bytes(path.read_bytes() + b' ')
            return result
        with patch.object(a, 'accepted', side_effect=accept), \
             self.assertRaisesRegex(ValueError, 'handoff_input_pin_changed'):
            admission.evidence(admission.start_action)

    def test_waiting_proof_still_repeats_startup_and_registration_checks(self):
        value = fixture(self, waiting=True)
        admission, foundation = operator(value)
        admission.policy_ref = value.save({'synthetic': 'policy'})
        process = {'pid': 1}; barrier = value.save({'synthetic': 'barrier'})
        launch = value.save(dict(owned_barrier=barrier, process=process))
        admission.inputs['launch'] = launch
        registration = value.save(dict(controller=dict(process=process, launch_receipt=launch), donor_histories=[]))
        ready = dict(at=h.now(), policy=admission.policy_ref, launch=launch, owned_barrier=barrier,
            required_sync_roots=[], evidence_pins=[], registration=registration,
            authorization_pointer=value.save(dict(registration=registration)), monitor_policy=value.save({}),
            **{key: True for key in ('zero_initial_claims', 'zero_persisted_blocks', 'legacy_owners_healthy',
                                     'mandatory_companion_valid', 'snapshot_ownership_valid')})
        ref = value.save(ready)
        admission.inputs['startup'] = dict(evidence=ref, review=value.save(
            dict(accepted_native_startup_consumers=True, readiness=ref)))
        admission.policy['required_sync_roots'] = []
        with patch.object(a, '_native_reservation_foreign_blocks_proof',
                          wraps=a._native_reservation_foreign_blocks_proof) as proof, \
             patch.object(a, 'validate_registration', return_value=True) as registration_check:
            self.assertEqual(admission.evidence(admission.release_action), foundation)
            self.assertEqual(admission.evidence(admission.release_action), foundation)
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(registration_check.call_count, 2)
            path = Path(admission.inputs['startup']['review']['path'])
            path.write_bytes(path.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'handoff_input_pin_changed'):
                admission.evidence(admission.release_action)

    def test_exported_allocation_dependencies_are_copies_on_cold_and_hot_calls(self):
        value = fixture(self)
        cache = s.NativeAllocationCache()
        exported = []
        def receive(ref):
            exported.append(copy.deepcopy(ref))
            ref['sha256'] = '0' * 64
        for _ in range(2):
            result = s.native_allocation(value.allocation, cache=cache, dependency_sink=receive)
            self.assertEqual(result[0], value.jobs)
        self.assertEqual(len(exported), 16)
        self.assertEqual(exported[:8], exported[8:])
        for ref in exported:
            h.checked_bytes(ref)
