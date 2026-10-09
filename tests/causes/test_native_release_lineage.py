"""Real two-link registration validation with invocation-local proof reuse."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h, campaign_monitor as m
from causes import campaign_corpus_succession as s, campaign_release as r
from causes.corpus_continuation import CorpusContinuation
from tests.causes import test_native_release_operator as original


class TwoLinkFixture(original.ReleaseFixture):
    def lineage(self):
        policy, registration = super().lineage()
        final = policy['native_successions']['links'][-1]
        value = h.checked(final['allocation']['evidence'])
        control = h.checked(value['allocation_control'])
        control['family'] = 'earlier-native-family'
        control_ref = self.save('earlier/control.json', control)
        allocator = CorpusContinuation.from_control(control, h.checked(control['prototypes']), h.checked(control['config']))
        jobs = allocator.block(0)['jobs']
        value.update(family=control['family'], output_root=str(self.root/'earlier-native'),
                     allocation_control=control_ref, jobs=self.save('earlier/jobs.json', jobs),
                     selection=dict(block_index=0, run_ids=[j['allocation']['run_id'] for j in jobs]))
        allocation_ref = self.save('earlier/allocation.json', value)
        allocation_pair = dict(evidence=allocation_ref, review=self.save('earlier/allocation-review.json',
            dict(accepted_native_corpus_allocation=True, allocation=allocation_ref)))
        prior = copy.deepcopy(policy['phases'][-1])
        prior.update(phase='earlier-native', output_root=value['output_root'],
                     runtime_cohort=value['family'], controller_jobs=value['jobs'])
        prior['process'].update(pid=778, process_starttime_ticks='778')
        for flag, argument in (('--output', value['output_root']), ('--jobs', value['jobs']['path'])):
            prior['process']['argv'][prior['process']['argv'].index(flag)+1] = argument
        journal = self.root/'earlier-native/.dispatcher/journal.jsonl'
        journal.parent.mkdir(parents=True)
        journal.write_bytes(b'{"event":"controller_paused"}\n')
        pause = self.save('earlier/pause.json', {'fixture':'owned-pause'})
        launch = self.save('earlier/launch.json', {'process':prior['process']})
        ownership = self.save('earlier/ownership.json', dict(process=prior['process'], marker=pause,
                                                           marker_stat=h.stat_identity(pause['path'])))
        pending = self.save('earlier/pending.json', sorted(j['allocation']['run_id'] for j in jobs))
        closure = self.save('earlier/closure.json', dict(schema='causes.closed_issued_corpus_history.v1',
            output_root=prior['output_root'], process=prior['process'], launch=launch, claims=0, finals=0,
            active_claims=0, ambiguous_ids=[], owner_absent=True, output_lock_free=True,
            pending_count=len(jobs), never_claimed=pending, journal=h.pin(journal), owned_pause=pause,
            owned_pause_stat=h.stat_identity(pause['path']), pause_ownership=ownership))
        predecessor = dict(phase=prior['phase'], output_root=prior['output_root'], process=prior['process'],
            launch=launch, pause_path=pause['path'], closure=dict(evidence=closure,
                review=self.save('earlier/closure-review.json', dict(accepted_closed_issued_corpus_history=True, closure=closure))))
        earlier_link = dict(phase=prior['phase'], predecessor=copy.deepcopy(final['predecessor']),
            allocation=allocation_pair, root_adoption=self.save('earlier/adoption.json',
                dict(root_accepted=True, allocation=allocation_ref, allocation_review=allocation_pair['review'])))
        final['predecessor'] = predecessor
        policy['native_successions']['links'].insert(0, earlier_link)
        policy['phases'].insert(len(policy['phases'])-1, prior)
        policy['sync_roots'].insert(len(policy['sync_roots'])-1, str(Path(prior['output_root']).relative_to(self.root)))
        registration['prior_local_owners'].append({k:prior[k] for k in ('output_root','process')})
        donor = dict(host=policy['controller_host'], output_root=prior['output_root'], mode='sealed',
                     recorder_relevant=True, journal=h.pin(journal))
        registration['donor_histories'].append(donor)
        policy['phases'][-1]['donor_histories'] = copy.deepcopy(registration['donor_histories'])
        policy['history']['donor_scopes'].append({k:donor[k] for k in ('host','output_root','mode','recorder_relevant')})
        return policy, registration


class ReleaseLineageTests(unittest.TestCase):
    def fixture(self):
        return TwoLinkFixture(self)

    def validate(self, policy, registration, bank, cache=None):
        return r.validate_release_registration(registration, policy, h.checked,
            allocation_ref=policy['native_successions']['links'][-1]['allocation']['evidence'],
            allocation_cache=cache if cache is not None else s.NativeAllocationCache(), lineage_caches=bank)

    def test_two_link_full_registration_parity_and_five_pass_build_count(self):
        policy, registration = self.fixture().lineage()
        expected = m.validate_registration(registration, policy, h.checked)
        bank, cache = {}, s.NativeAllocationCache()
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as build:
            for _ in range(5):
                self.assertEqual(self.validate(policy, registration, bank, cache), expected)
            self.assertEqual(build.call_count, 2)
        self.assertEqual(list(bank), [h.encoded(policy['native_successions']['links'][0]['allocation']['evidence'])])
        self.assertIsNot(next(iter(bank.values())), cache)

    def test_omitted_bank_keeps_previous_six_build_behavior(self):
        policy, registration = self.fixture().lineage()
        cache = s.NativeAllocationCache()
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as build:
            for _ in range(5):
                self.validate(policy, registration, None, cache)
            self.assertEqual(build.call_count, 6)

    def test_whole_evidence_passes_use_one_operator_bank_and_recheck_closures(self):
        operator, policy, registration, foundation = self.fixture().operator()
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as build:
            for _ in range(5):
                self.assertEqual(operator.evidence(operator.release_action), foundation)
            self.assertEqual(build.call_count, 2)
        earlier = policy['native_successions']['links'][0]['allocation']['evidence']
        self.assertEqual(list(operator._release_lineage_caches), [h.encoded(earlier)])
        closure = policy['native_successions']['links'][-1]['predecessor']['closure']['evidence']
        path = Path(closure['path']); path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(ValueError):
            operator.evidence(operator.release_action)

    def test_earlier_dependency_drift_permanently_invalidates_only_its_cache(self):
        policy, registration = self.fixture().lineage()
        bank, cache = {}, s.NativeAllocationCache()
        self.validate(policy, registration, bank, cache)
        earlier = h.checked(policy['native_successions']['links'][0]['allocation']['evidence'])
        path = Path(earlier['jobs']['path']); before = path.read_bytes()
        path.write_bytes(before+b' ')
        with self.assertRaises(ValueError):
            self.validate(policy, registration, bank, cache)
        path.write_bytes(before)
        with self.assertRaisesRegex(ValueError, 'native_allocation_cache_invalid'):
            self.validate(policy, registration, bank, cache)
        final = h.checked(policy['native_successions']['links'][-1]['allocation']['evidence'])
        s.native_allocation(final, cache=cache)

    def test_cache_hit_preserves_lineage_and_registration_rejections(self):
        policy, registration = self.fixture().lineage()
        bank, cache = {}, s.NativeAllocationCache()
        self.validate(policy, registration, bank, cache)
        changes = [lambda p,x:p['native_successions']['links'][0].update(phase='wrong'),
                   lambda p,x:p['phases'][-2].update(workers=3),
                   lambda p,x:x['prior_local_owners'][-1]['process'].update(pid=101),
                   lambda p,x:x['donor_histories'].pop(),
                   lambda p,x:x['controller'].update(workers=3)]
        for change in changes:
            p, reg = copy.deepcopy(policy), copy.deepcopy(registration)
            change(p, reg)
            failures = []
            for call in (lambda:m.validate_registration(reg,p,h.checked),
                         lambda:self.validate(p,reg,bank,cache)):
                with self.assertRaises(ValueError) as error:
                    call()
                failures.append(str(error.exception))
            self.assertEqual(*failures)

    def test_bank_is_per_operator_and_type_scope_is_explicit(self):
        first, second = original.ReleaseFixture(self).bind(), original.ReleaseFixture(self).bind()
        self.assertIsNot(first._release_lineage_caches, second._release_lineage_caches)
        first._release_lineage_caches[b'test'] = object()
        self.assertEqual(second._release_lineage_caches, {})
        policy, registration = self.fixture().lineage()
        with self.assertRaisesRegex(ValueError, 'release_lineage_cache_scope_changed'):
            self.validate(policy, registration, [])


if __name__ == '__main__':
    unittest.main()
