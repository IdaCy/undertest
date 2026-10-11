"""Adversarial checks of cache-bank boundaries in a real two-link lineage."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h, campaign_release as r
from causes import campaign_corpus_succession as s
from causes.corpus_continuation import CorpusContinuation
from tests.causes import test_native_release_lineage as fixtures


class ReleaseLineageReviewTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.TwoLinkFixture(self)
        policy, registration = fixture.lineage()
        return fixture, policy, registration, {}, s.NativeAllocationCache()

    def validate(self, policy, registration, bank, cache, reader=h.checked):
        return r.validate_release_registration(registration, policy, reader,
            allocation_ref=policy['native_successions']['links'][-1]['allocation']['evidence'],
            allocation_cache=cache, lineage_caches=bank)

    def test_earlier_symlink_failure_permanently_invalidates_warm_entry(self):
        _, policy, registration, bank, cache = self.fixture()
        self.validate(policy, registration, bank, cache)
        earlier = h.checked(policy['native_successions']['links'][0]['allocation']['evidence'])
        path = Path(earlier['jobs']['path'])
        moved = path.with_name('moved-jobs.json')
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(ValueError):
            self.validate(policy, registration, bank, cache)
        path.unlink()
        moved.rename(path)
        with self.assertRaisesRegex(ValueError, 'native_allocation_cache_invalid'):
            self.validate(policy, registration, bank, cache)

    def test_earlier_reviews_and_adoption_remain_checked_on_hits(self):
        _, policy, registration, bank, cache = self.fixture()
        expected = self.validate(policy, registration, bank, cache)
        link = policy['native_successions']['links'][0]
        for ref in (link['allocation']['review'], link['root_adoption']):
            with self.subTest(path=ref['path']):
                path = Path(ref['path'])
                raw = path.read_bytes()
                path.write_bytes(raw + b' ')
                with self.assertRaises(ValueError):
                    self.validate(policy, registration, bank, cache)
                path.write_bytes(raw)
                self.assertEqual(self.validate(policy, registration, bank, cache), expected)

    def test_equal_bytes_at_new_reference_get_independent_reconstruction(self):
        fixture, policy, registration, bank, cache = self.fixture()
        expected = self.validate(policy, registration, bank, cache)
        old_link = policy['native_successions']['links'][0]
        old_ref = old_link['allocation']['evidence']
        new_ref = fixture.save('renamed/allocation.json', h.checked(old_ref))
        self.assertEqual((new_ref['bytes'], new_ref['sha256']), (old_ref['bytes'], old_ref['sha256']))
        replacement = copy.deepcopy(policy)
        link = replacement['native_successions']['links'][0]
        link['allocation'] = dict(evidence=new_ref, review=fixture.save('renamed/review.json',
            dict(accepted_native_corpus_allocation=True, allocation=new_ref)))
        link['root_adoption'] = fixture.save('renamed/adoption.json', dict(root_accepted=True,
            allocation=new_ref, allocation_review=link['allocation']['review']))
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as build:
            self.assertEqual(self.validate(replacement, registration, bank, cache), expected)
            self.assertEqual(build.call_count, 1)
        self.assertEqual(len(bank), 2)
        self.assertIsNot(bank[h.encoded(old_ref)], bank[h.encoded(new_ref)])

    def test_noncanonical_reader_refuses_before_build_or_bank_fill(self):
        _, policy, registration, bank, cache = self.fixture()
        with patch.object(CorpusContinuation, 'from_control', wraps=CorpusContinuation.from_control) as build:
            with self.assertRaisesRegex(ValueError, 'release_allocation_cache_scope_changed'):
                self.validate(policy, registration, bank, cache, reader=lambda ref: h.checked(ref))
            self.assertEqual(build.call_count, 0)
        self.assertEqual(bank, {})

    def test_mutating_earlier_return_does_not_poison_later_registration(self):
        _, policy, registration, bank, cache = self.fixture()
        expected = self.validate(policy, registration, bank, cache)
        earlier_ref = policy['native_successions']['links'][0]['allocation']['evidence']
        jobs, config, _, allocator = s.native_allocation(h.checked(earlier_ref), cache=bank[h.encoded(earlier_ref)])
        jobs[0]['spec']['task_id'] = 'mutated-task'
        config['unexpected'] = True
        allocator.used_seeds.clear()
        self.assertEqual(self.validate(policy, registration, bank, cache), expected)


if __name__ == '__main__':
    unittest.main()
