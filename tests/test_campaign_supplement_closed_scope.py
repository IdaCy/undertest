import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct
from causes import campaign_supplement_closed_scope as scope
from test_campaign_supplement_original_transfer import TransferFixture


class ClosedScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = TransferFixture(Path(self.temp.name))

    def same_bank(self, count=2):
        f = self.f
        jobs = f.bank_jobs['texas']
        previous = dict(schema='causes.finite_supplement_tranche_closed.v1',
            full_jobs=f.save(jobs), complete_ids=[jobs[0]['measurement_id']],
            unstarted_ids=[j['measurement_id'] for j in jobs[1:]], terminal=1)
        plan = dict(family=f.family, model='mi27', occupied_ids=previous['complete_ids'],
            maximum_new_requests=count, jobs_sha256=core.digest(jobs[1:1+count]),
            maximum_admission_attempts=8, maximum_captures=4, maximum_capture_uses=8)
        return plan, previous, jobs

    def transfer(self):
        f = self.f
        f.previous.update(schema='causes.finite_supplement_tranche_closed.v1', terminal=1)
        f.plan.update(occupied_ids=f.previous['complete_ids'], maximum_new_requests=2,
                      maximum_admission_attempts=8, maximum_captures=2, maximum_capture_uses=8)
        return scope.resolve(f.plan, f.previous, original_transfer_jobs=direct.original_transfer_jobs)

    def test_complete_same_bank_reuses_exact_full_jobs_pin(self):
        plan, previous, jobs = self.same_bank()
        found = scope.resolve(plan, previous)
        self.assertEqual(found['jobs'], jobs[1:])
        self.assertEqual(found['full_jobs'], jobs)
        self.assertIsNone(scope.bind_extension(found, previous['full_jobs']))
        ids = [j['measurement_id'] for j in jobs[1:]]
        result = scope.partition(found, ids, list(reversed(ids)))
        self.assertEqual(result['complete_ids'], [j['measurement_id'] for j in jobs])
        self.assertEqual((result['terminal'], result['unstarted_ids']), (3, []))

    def test_bounded_qualification_preserves_later_original_suffix(self):
        plan, previous, jobs = self.same_bank(1)
        found = scope.resolve(plan, previous)
        result = scope.partition(found, [jobs[1]['measurement_id']], [jobs[1]['measurement_id']])
        self.assertEqual(result['unstarted_ids'], [jobs[2]['measurement_id']])

    def test_unresolved_claim_stays_occupied_and_not_terminal(self):
        plan, previous, jobs = self.same_bank()
        result = scope.partition(scope.resolve(plan, previous), [jobs[1]['measurement_id']], [])
        self.assertEqual(result['terminal'], 1)
        self.assertEqual(result['occupied'], 2)
        self.assertEqual(result['unresolved_claimed_ids'], [jobs[1]['measurement_id']])
        self.assertEqual(result['unstarted_ids'], [jobs[2]['measurement_id']])

    def test_reordered_prior_partition_refuses(self):
        plan, previous, jobs = self.same_bank()
        previous['unstarted_ids'].reverse()
        with self.assertRaisesRegex(core.IntegrityError, 'prior_partition'):
            scope.resolve(plan, previous)

    def test_wrong_current_digest_refuses(self):
        plan, previous, jobs = self.same_bank()
        plan['jobs_sha256'] = '0'*64
        with self.assertRaisesRegex(core.IntegrityError, 'current_jobs'):
            scope.resolve(plan, previous)

    def test_requested_jobs_cannot_exceed_original_suffix(self):
        plan, previous, jobs = self.same_bank()
        plan['maximum_new_requests'] = 3
        with self.assertRaisesRegex(core.IntegrityError, 'current_jobs'):
            scope.resolve(plan, previous)

    def test_claim_order_and_unknown_final_refuse(self):
        plan, previous, jobs = self.same_bank()
        found = scope.resolve(plan, previous)
        for claimed, terminal in [([jobs[2]['measurement_id']], []),
                                  ([jobs[1]['measurement_id']], [jobs[2]['measurement_id']])]:
            with self.assertRaisesRegex(core.IntegrityError, 'current_partition'):
                scope.partition(found, claimed, terminal)

    def test_transfer_appends_exact_jobs_and_preserves_origin(self):
        found = self.transfer()
        self.assertEqual(found['jobs'], self.f.selected)
        self.assertEqual(len(found['full_jobs']), 3)
        self.assertEqual(found['extension']['transfer'], self.f.plan['original_reservation_transfer'])
        data = core.encoded(found['full_jobs'])
        ref = dict(path=str(self.f.root/'FULL_JOBS.json'), bytes=len(data),
                   sha256=hashlib.sha256(data).hexdigest())
        extension = scope.bind_extension(found, ref)
        self.assertEqual(extension['schema'], scope.EXTENSION_SCHEMA)
        self.assertEqual(extension['full_jobs'], ref)
        self.assertTrue(extension['prior_prefix_unchanged'])

    def test_transfer_requires_explicit_reviewed_resolver(self):
        self.transfer()
        with self.assertRaisesRegex(core.IntegrityError, 'transfer_resolver_required'):
            scope.resolve(self.f.plan, self.f.previous)

    def test_extended_full_jobs_wrong_bytes_refuse(self):
        found = self.transfer()
        bad = dict(path=str(self.f.root/'FULL_JOBS.json'), bytes=1, sha256='0'*64)
        with self.assertRaisesRegex(core.IntegrityError, 'extended_bank_bytes'):
            scope.bind_extension(found, bad)

    def test_ordinary_full_jobs_change_between_calls_refuses(self):
        plan, previous, jobs = self.same_bank()
        scope.resolve(plan, previous)
        Path(previous['full_jobs']['path']).write_bytes(b'[]')
        with self.assertRaises(core.IntegrityError):
            scope.resolve(plan, previous)

    def test_invalid_budget_types_refuse(self):
        for key in ('maximum_new_requests', 'maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses'):
            plan, previous, jobs = self.same_bank()
            plan[key] = True
            with self.subTest(key=key), self.assertRaises(core.IntegrityError):
                scope.resolve(plan, previous)


if __name__ == '__main__':
    unittest.main()
