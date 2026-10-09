"""Real synthetic allocation proofs remain exact while admission reuses work."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch

from causes import campaign_admission as a
from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from causes.corpus_continuation import CorpusContinuation
from tests.causes import test_native_corpus_succession as native_fixture
from tests.causes import test_native_reservation_growth as growth_fixture


class AllocationReuseTests(unittest.TestCase):
    setUp = native_fixture.NativeAllocationTests.setUp
    save = native_fixture.NativeAllocationTests.save
    allocation = native_fixture.NativeAllocationTests.allocation
    runtime = native_fixture.NativeAllocationTests.runtime

    def test_cached_finite_mixed_and_prefix_outputs_equal_uncached(self):
        for finite, prefix in ((True, False), (False, False), (False, True)):
            with self.subTest(finite=finite, prefix=prefix):
                value = self.allocation(finite, prefix)
                original = s.native_allocation(value)
                cache = s.NativeAllocationCache()
                with patch.object(CorpusContinuation, 'from_control',
                                  wraps=CorpusContinuation.from_control) as reconstruction:
                    first = s.native_allocation(value, cache=cache)
                    second = s.native_allocation(value, cache=cache)
                self.assertEqual(reconstruction.call_count, 1)
                for result in (first, second):
                    self.assertEqual(result[:3], original[:3])
                    self.assertEqual(result[3].__dict__, original[3].__dict__)

    def test_runtime_reuses_validation_but_rechecks_command_scope(self):
        value, template, common, expected = self.runtime()
        cache = s.NativeAllocationCache()
        with patch.object(CorpusContinuation, 'from_control',
                          wraps=CorpusContinuation.from_control) as reconstruction:
            self.assertEqual(a.native_runtime(value, template, common,
                                              allocation_cache=cache), expected)
            self.assertEqual(a.native_runtime(value, template, common,
                                              allocation_cache=cache), expected)
            bad = copy.deepcopy(template)
            bad['argv_template_at_proposed_cap'][-1] = 'changed'
            with self.assertRaisesRegex(ValueError, 'native_command_scope_changed'):
                a.native_runtime(value, bad, common, allocation_cache=cache)
        self.assertEqual(reconstruction.call_count, 1)

    def test_uncached_default_and_independent_instances_do_not_share_results(self):
        value = self.allocation()
        with patch.object(CorpusContinuation, 'from_control',
                          wraps=CorpusContinuation.from_control) as reconstruction:
            s.native_allocation(value)
            s.native_allocation(value)
            s.native_allocation(value, cache=s.NativeAllocationCache())
            s.native_allocation(value, cache=s.NativeAllocationCache())
        self.assertEqual(reconstruction.call_count, 4)

    def test_missing_dependency_invalidates_without_reconstruction_or_refresh(self):
        from pathlib import Path
        value = self.allocation()
        cache = s.NativeAllocationCache()
        s.native_allocation(value, cache=cache)
        path = Path(h.checked(value['allocation_control'])['reservation']['path'])
        saved = path.read_bytes()
        path.unlink()
        with patch.object(CorpusContinuation, 'from_control',
                          side_effect=AssertionError('must not reconstruct')):
            with self.assertRaises(FileNotFoundError):
                s.native_allocation(value, cache=cache)
            path.write_bytes(saved)
            with self.assertRaisesRegex(ValueError, 'cache_invalid'):
                s.native_allocation(value, cache=cache)


class AdmissionReuseTests(unittest.TestCase):
    setUp = native_fixture.NativeAdmissionTests.setUp
    save = native_fixture.NativeAdmissionTests.save
    closure = native_fixture.NativeAdmissionTests.closure
    allocation = native_fixture.NativeAdmissionTests.allocation
    runtime = native_fixture.NativeAdmissionTests.runtime
    operator = native_fixture.NativeAdmissionTests.operator

    def test_whole_static_shares_runtime_and_route_proof_and_rechecks_policy(self):
        operator, _ = self.operator()
        with patch.object(CorpusContinuation, 'from_control',
                          wraps=CorpusContinuation.from_control) as reconstruction:
            first = operator.static()
            self.assertEqual(operator.static(), first)
            self.assertEqual(reconstruction.call_count, 1)
            operator.policy['workers'] += 1
            with self.assertRaisesRegex(ValueError, 'native_reviewed_policy_changed'):
                operator.static()
            self.assertEqual(reconstruction.call_count, 1)

    def test_warm_cache_cannot_hide_changed_independent_acceptance(self):
        operator, _ = self.operator()
        operator.static()
        reference = operator.inputs['source_review']
        from pathlib import Path
        path = Path(reference['path'])
        path.write_bytes(path.read_bytes().replace(b'true', b'null', 1))
        with self.assertRaises(ValueError):
            operator.static()


class EvidenceReuseTests(unittest.TestCase):
    def test_start_and_release_reuse_only_allocation_and_keep_freshness_checks(self):
        fixture = growth_fixture.NativeReservationGrowthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.build()
        operator, foundation = fixture.operator(fixture.old, fixture.current,
                                                fixture.allocation, fixture.config_ref)
        operator.policy.update(reservation_transition='exact_own_and_foreign_blocks_v1',
                               reservation_growth=fixture.growth,
                               census_hosts=['fixture', 'other'])

        def reservation_inputs():
            reference = fixture.save(fixture.current, 'fresh')
            peer = h.checked(operator.inputs['fresh_reservation']['review'])
            peer.update(evidence=reference, census_config=fixture.config_ref,
                        accepted_dual_host_capture_custody=True,
                        accepted_native_foreign_block_extension=True,
                        generation_reservation=operator.inputs['precreation_reservation']['evidence'],
                        reservation_growth=fixture.growth,
                        capture_pins=fixture.current['capture_pins'])
            operator.inputs['fresh_reservation'] = dict(evidence=reference, review=fixture.save(peer))
            operator.policy['census_configs']['waiting_release'] = fixture.config_ref

        reservation_inputs()
        with patch.object(CorpusContinuation, 'from_control',
                          wraps=CorpusContinuation.from_control) as reconstruction:
            self.assertEqual(operator.evidence(operator.start_action), foundation)
            self.assertEqual(operator.evidence(operator.start_action), foundation)
            fixture.refresh(waiting=True)
            reservation_inputs()
            operator.policy_ref = fixture.save({'synthetic': 'policy'})
            process = {'pid': 1}
            barrier = fixture.save({'synthetic': 'barrier'})
            launch = fixture.save(dict(owned_barrier=barrier, process=process))
            operator.inputs['launch'] = launch
            registration = fixture.save(dict(controller=dict(process=process, launch_receipt=launch),
                                             donor_histories=[]))
            pointer = fixture.save(dict(registration=registration))
            ready = dict(at=h.now(), policy=operator.policy_ref, launch=launch, owned_barrier=barrier,
                         required_sync_roots=[], evidence_pins=[], registration=registration,
                         authorization_pointer=pointer, monitor_policy=fixture.save({}),
                         **{key: True for key in ('zero_initial_claims', 'zero_persisted_blocks',
                            'legacy_owners_healthy', 'mandatory_companion_valid', 'snapshot_ownership_valid')})
            reference = fixture.save(ready)
            operator.inputs['startup'] = dict(evidence=reference, review=fixture.save(
                dict(accepted_native_startup_consumers=True, readiness=reference)))
            operator.policy['required_sync_roots'] = []
            with patch.object(a, 'validate_registration', return_value=True) as registration_check:
                self.assertEqual(operator.evidence(operator.release_action), foundation)
                self.assertEqual(operator.evidence(operator.release_action), foundation)
                self.assertEqual(registration_check.call_count, 2)
            self.assertEqual(reconstruction.call_count, 1)
            stale = copy.deepcopy(foundation)
            stale['at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=181)).isoformat()
            ref = fixture.save(stale)
            peer = h.checked(operator.inputs['foundation']['review'])
            peer['evidence'] = ref
            operator.inputs['foundation'] = dict(evidence=ref, review=fixture.save(peer))
            with self.assertRaisesRegex(ValueError, 'admission_observation_stale'):
                operator.evidence(operator.release_action)
            self.assertEqual(reconstruction.call_count, 1)
