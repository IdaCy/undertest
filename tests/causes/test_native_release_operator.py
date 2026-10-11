"""Whole real synthetic registration/evidence bodies over an unchanged runtime."""
import ast
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_admission as a, campaign_census as census
from causes import campaign_corpus_succession as s, campaign_handoff as h
from causes import campaign_monitor as m, campaign_release as r
from causes.corpus_continuation import CorpusContinuation
from tests.causes import test_native_corpus_succession as native


class ReleaseFixture:
    save = native.ClosedIssuedTests.save
    closure = native.ClosedIssuedTests.closure
    runtime = native.NativeAllocationTests.runtime

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.at = h.now()

    def allocation(self, finite=True, prefix=False):
        value = native.NativeAllocationTests.allocation(self, finite, prefix)
        control = h.checked(value['allocation_control'])
        reservation = h.checked(control['reservation'])
        jobs = h.checked(control['prototypes']) + h.checked(control['original_jobs'])
        reservation.update(allocation_identities=[census.identity(j['allocation']) for j in jobs],
                           run_ids=sorted(reservation['run_ids']), frozen_input_pins=[],
                           host_cuts=[dict(host='fixture', started_at=self.at, finished_at=self.at)])
        control['reservation'] = self.save('allocation/reservation.json', reservation)
        value['allocation_control'] = self.save('allocation/control.json', control)
        allocator = CorpusContinuation.from_control(control, h.checked(control['prototypes']), h.checked(control['config']))
        jobs = allocator.block(0)['jobs'] if finite else []
        value['jobs'] = self.save('allocation/jobs.json', jobs)
        if finite:
            value['selection']['run_ids'] = [j['allocation']['run_id'] for j in jobs]
        else:
            value['control'] = value['allocation_control']
        return value

    def lineage(self):
        policy, registration, objects, _ = native.NativeMonitorTests.lineage(self)
        for index, donor in enumerate(registration['donor_histories']):
            path = Path(donor['output_root']) / '.dispatcher/journal.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{"event":"controller_paused"}\n')
            donor['journal'] = h.pin(path)
            policy['phases'][-1]['donor_histories'][index]['journal'] = h.pin(path)
        closure = policy['native_successions']['links'][-1]['predecessor']['closure']['evidence']
        objects[closure['path']]['journal'] = registration['donor_histories'][-1]['journal']
        mapped = {}
        def materialize(value):
            if isinstance(value, dict):
                if set(value) == {'path', 'bytes', 'sha256'} and value['path'] in objects:
                    key = value['path']
                    if key not in mapped:
                        mapped[key] = self.save('metadata/' + Path(key).name, materialize(objects[key]))
                    return mapped[key]
                return {k: materialize(v) for k, v in value.items()}
            if isinstance(value, list):
                return [materialize(v) for v in value]
            return value
        return materialize(policy), materialize(registration)

    def bind(self, policy=None, inputs=None):
        bases = {module.__name__: h.pin(module.__file__) for module in r.BASE_MODULES}
        proof = self.save('operator-proof.json', dict(contract=r.CONTRACT, source=h.pin(r.__file__),
            base_sources=bases, whole_function_inverse=dict(evidence=True, registration=True, lineage=True)))
        peer = self.save('operator-review.json', dict(accepted_native_release_operator_sources=True,
            source=h.pin(r.__file__), base_sources=bases, contract=r.CONTRACT, inverse_proof=proof))
        policy = copy.deepcopy(policy) if policy is not None else {'operator_module_hashes': {k:v['sha256'] for k,v in bases.items()}}
        inputs = copy.deepcopy(inputs) if inputs is not None else {}
        inputs['release_operator'] = dict(source=h.pin(r.__file__), review=peer)
        pref = self.save('release-policy.json', policy)
        inputs['policy'] = pref
        return r.ReleaseOnlyNativeAdmission(pref, self.save('release-inputs.json', inputs), {})

    def operator(self):
        original, _ = native.NativeAdmissionTests.operator(self)
        monitor, registration = self.lineage()
        p, x = copy.deepcopy(original.policy), copy.deepcopy(original.inputs)
        base_ref = monitor['native_successions']['base_policy']; base = h.checked(base_ref)
        base_peer = self.save('base-native-review.json', dict(accepted_native_predecessor_monitor=True, policy=base_ref))
        p['predecessor_monitor'] = x['predecessor_monitor'] = dict(evidence=base_ref, review=base_peer)
        for key in ('provider', 'model', 'model_key', 'controller_host', 'provider_port', 'max_model_len', 'host_transfer', 'holds'):
            p[key] = base[key]
        p['predecessors'] = [{k:phase[k] for k in ('output_root', 'process')} for phase in base['phases']]
        p['predecessors'][-1] = monitor['native_successions']['links'][-1]['predecessor']
        p['history'] = monitor['history']; p['donor_histories'] = registration['donor_histories']
        p['legacy_owners'] = []; p['census_hosts'] = ['fixture']
        cfg = self.save('census.json', {})
        p['census_configs'] = dict(before_start=cfg, waiting_release=cfg)
        p['required_sync_roots'] = {'mi': [], 'jp': monitor['sync_roots']}
        allocation = h.checked(p['allocation']); old_ref = h.checked(allocation['allocation_control'])['reservation']
        cuts = [dict(row, capture=row['journal'], at=self.at) for row in p['donor_histories']]
        foundation = dict(at=self.at, donor_cuts=cuts, evidence_pins=[],
            **{key:p[key] for key in ('provider', 'host_transfer', 'legacy_owners', 'predecessors')},
            **{key:True for key in ('legacy_owners_healthy', 'provider_tunnels_healthy', 'original_transfer_present',
                'donor_histories_clear', 'native_no_active_owned_work', 'all_predecessors_absent_and_unlocked')})
        for key, ref in [('precreation_reservation', old_ref), ('fresh_reservation', old_ref),
                         ('foundation', self.save('foundation.json', foundation))]:
            flag = 'accepted_' + key
            review = dict(evidence=ref, **{flag:True})
            if key == 'fresh_reservation': review['census_config'] = cfg
            x[key] = dict(evidence=ref, review=self.save(key+'-review.json', review))
            p['acceptance_rules'][key] = dict(flag=flag, binding_key='evidence')
        old_template = h.checked(p['policy_template']); dynamic = old_template['dynamic_keys']
        template = dict(schema=s.TEMPLATE_SCHEMA, fixed={k:v for k,v in p.items() if k not in dynamic and k != 'policy_template'}, dynamic_keys=dynamic)
        p['policy_template'] = self.save('release-template.json', template)
        x['source_review'] = self.save('release-source-review.json', dict(accepted_native_corpus_admission_source=True,
            accepted_native_corpus_policy_template=True, policy_template=p['policy_template'], source=h.pin(a.__file__)))
        pref = self.save('release-policy.json', p)
        barrier = self.save('barrier.json', {'fixture':'owned'})
        launch = self.save('launch-native.json', dict(policy=pref, process=registration['controller']['process'], owned_barrier=barrier))
        registration['controller']['launch_receipt'] = launch
        reg_ref = self.save('registration.json', registration)
        ready = dict(at=self.at, policy=pref, launch=launch, owned_barrier=barrier,
            required_sync_roots=p['required_sync_roots'], evidence_pins=[], registration=reg_ref,
            authorization_pointer=self.save('pointer.json', dict(registration=reg_ref)),
            monitor_policy=self.save('monitor-policy.json', monitor),
            **{key:True for key in ('zero_initial_claims', 'zero_persisted_blocks', 'legacy_owners_healthy',
                'mandatory_companion_valid', 'snapshot_ownership_valid')})
        ready_ref = self.save('startup.json', ready)
        x['startup'] = dict(evidence=ready_ref, review=self.save('startup-review.json', dict(accepted_native_startup_consumers=True, readiness=ready_ref)))
        x['launch'] = launch
        operator = self.bind(p, x)
        assert operator.policy_ref == pref
        return operator, monitor, registration, foundation


class ReleaseOperatorTests(unittest.TestCase):
    def setUp(self): self.fixture = ReleaseFixture(self)

    def test_full_registration_matches_original_and_reuses_only_allocation(self):
        policy, registration = self.fixture.lineage()
        ref = policy['native_successions']['links'][-1]['allocation']['evidence']
        expected = m.validate_registration(registration, policy, h.checked)
        cache = s.NativeAllocationCache()
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as reconstruct:
            for _ in range(3):
                self.assertEqual(r.validate_release_registration(registration, policy, h.checked,
                    allocation_ref=ref, allocation_cache=cache), expected)
        self.assertEqual(reconstruct.call_count, 1)

    def test_whole_static_then_evidence_shares_cache_and_retains_registration(self):
        operator, policy, registration, foundation = self.fixture.operator()
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as reconstruct:
            operator.static()
            self.assertEqual(operator.evidence(operator.release_action), foundation)
            self.assertEqual(operator.evidence(operator.release_action), foundation)
            self.assertEqual(reconstruct.call_count, 1)
        registration['controller']['workers'] += 1
        with self.assertRaisesRegex(ValueError, 'native_full_capacity_changed'):
            r.validate_release_registration(registration, policy, h.checked,
                allocation_ref=operator.policy['allocation'], allocation_cache=operator._allocation_cache())

    def test_original_and_derivative_refuse_same_registration_mutations(self):
        policy, registration = self.fixture.lineage()
        changes = [lambda p,r:r.update(status='retired'), lambda p,r:r['controller'].update(workers=9),
                   lambda p,r:r.update(donor_histories=[]), lambda p,r:p['sync_roots'].reverse(),
                   lambda p,r:r['controller']['process']['argv'].__setitem__(
                       r['controller']['process']['argv'].index('--max-full')+1, '0')]
        for change in changes:
            p, reg = copy.deepcopy(policy), copy.deepcopy(registration); change(p, reg)
            ref = policy['native_successions']['links'][-1]['allocation']['evidence']
            failures = []
            for call in (lambda:m.validate_registration(reg,p,h.checked),
                         lambda:r.validate_release_registration(reg,p,h.checked,allocation_ref=ref,allocation_cache=s.NativeAllocationCache())):
                with self.assertRaises(ValueError) as error: call()
                failures.append(str(error.exception))
            self.assertEqual(*failures)

    def test_stale_foundation_cold_and_hot_always_refuses(self):
        for warm in (False, True):
            fixture = ReleaseFixture(self); operator, _, _, foundation = fixture.operator()
            if warm: operator.evidence(operator.release_action)
            foundation['at'] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat()
            x = copy.deepcopy(operator.inputs)
            ref = fixture.save('stale-foundation.json', foundation)
            x['foundation'] = dict(evidence=ref, review=fixture.save('stale-review.json', dict(accepted_foundation=True,evidence=ref)))
            # A new operator is needed for a changed input pin; retain cache to
            # isolate the already-verified pure allocation from freshness.
            changed = fixture.bind(operator.policy, x)
            if warm: changed._allocation_validation = operator._allocation_cache()
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'admission_observation_stale'):
                    changed.evidence(changed.release_action)

    def test_every_startup_gate_remains_live_after_cache_warm(self):
        operator, _, _, _ = self.fixture.operator(); operator.evidence(operator.release_action)
        ready = h.checked(operator.inputs['startup']['evidence']); x = copy.deepcopy(operator.inputs)
        ready['mandatory_companion_valid'] = False
        ref = self.fixture.save('bad-startup.json',ready)
        x['startup'] = dict(evidence=ref,review=self.fixture.save('bad-startup-review.json',dict(accepted_native_startup_consumers=True,readiness=ref)))
        changed = self.fixture.bind(operator.policy,x);changed._allocation_validation=operator._allocation_cache()
        with self.assertRaisesRegex(ValueError,'native_startup_gate_missing:mandatory_companion_valid'):
            changed.evidence(changed.release_action)

    def test_changed_nested_dependency_permanently_invalidates_shared_cache(self):
        operator, _, _, _ = self.fixture.operator();operator.static();operator.evidence(operator.release_action)
        allocation=h.checked(operator.policy['allocation']);control=h.checked(allocation['allocation_control'])
        path=Path(control['prototypes']['path']);before=path.read_bytes();path.write_bytes(before+b' ')
        with self.assertRaises(ValueError):operator.evidence(operator.release_action)
        path.write_bytes(before)
        with self.assertRaisesRegex(ValueError,'native_allocation_cache_invalid'):
            operator.evidence(operator.release_action)

    def test_inherited_release_and_all_live_authority_methods_are_exact(self):
        for name in ('static','live','authority','release','_allocation_cache','_reservation_cache'):
            self.assertIs(getattr(r.ReleaseOnlyNativeAdmission,name),getattr(a.NativeAdmission,name))


if __name__ == '__main__':
    unittest.main()
