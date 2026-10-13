import copy
import unittest

from causes import campaign_supplement as core
from causes.campaign_supplement_collect import journal_index
from causes.campaign_drained_supplement import drained_partition


class DrainedPartitionTest(unittest.TestCase):
    def setUp(self):
        self.jobs = [dict(measurement_id='m' + str(i), run_id='r' + str(i),
                          seed=i + 10, original_seed=i + 20, stage='early',
                          question_id='strong', request_body_sha256='body',
                          request_file_sha256='file', request_body_bytes=1,
                          context_canonical_sha256='context',
                          question_canonical_sha256='question',
                          native_job_sha256='native') for i in range(3)]
        self.history = []
        for i, event in enumerate(('route_success', 'route_failure')):
            mid = self.jobs[i]['measurement_id']
            self.history.extend([
                dict(event='claimed', measurement_id=mid,
                     input_sha256=core.digest(self.jobs[i])),
                dict(event=event, measurement_id=mid,
                     final=dict(path='/saved/' + mid, bytes=1, sha256='opaque'),
                     technical_status='complete' if i == 0 else 'invalid_answer_schema')])
        self.custody = dict(claimed_ids=['m0', 'm1'], terminal_ids=['m0', 'm1'],
                            rows=copy.deepcopy(self.history[1::2]))
        self.unstarted = [dict(self.jobs[2], ordinal=2,
                               input_sha256=core.digest(self.jobs[2]))]

    def call(self):
        return drained_partition(self.jobs, self.history, self.custody, self.unstarted,
                                 journal_index=journal_index, digest=core.digest)

    def test_failed_terminal_and_unstarted_are_preserved_without_mutation(self):
        before = copy.deepcopy((self.jobs, self.history, self.custody, self.unstarted))
        result = self.call()
        self.assertEqual(result['counts'], dict(assigned=3, terminal=2, unstarted=1,
                                               technical_failures=1))
        self.assertEqual(result['finals']['m1'], self.history[3])
        self.assertEqual(result['unstarted_ids'], {'m2'})
        self.assertEqual(before, (self.jobs, self.history, self.custody, self.unstarted))

    def test_unresolved_claim_refuses(self):
        self.history.pop()
        with self.assertRaisesRegex(ValueError, 'unresolved_drained_claim'):
            self.call()

    def test_duplicate_custody_inventory_refuses(self):
        self.custody['claimed_ids'].append('m0')
        with self.assertRaisesRegex(ValueError, 'custody_terminal_inventory'):
            self.call()

    def test_changed_final_refuses(self):
        self.custody['rows'][1]['technical_status'] = 'complete'
        with self.assertRaisesRegex(ValueError, 'custody_terminal_identity'):
            self.call()

    def test_removed_failed_terminal_refuses(self):
        self.custody['rows'].pop()
        with self.assertRaisesRegex(ValueError, 'custody_terminal_rows'):
            self.call()

    def test_changed_claim_input_refuses(self):
        self.history[0]['input_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'drained_claim_input_identity'):
            self.call()

    def test_unstarted_original_identity_is_type_sensitive(self):
        for field, value in (('seed', 12.0), ('request_body_bytes', True),
                             ('question_canonical_sha256', 'changed')):
            with self.subTest(field=field):
                saved = self.unstarted[0][field]
                self.unstarted[0][field] = value
                with self.assertRaisesRegex(ValueError, 'unstarted_original_input_identity'):
                    self.call()
                self.unstarted[0][field] = saved

    def test_false_or_changed_ordinal_refuses(self):
        for ordinal in (True, -1, 3, 1):
            with self.subTest(ordinal=ordinal):
                self.unstarted[0]['ordinal'] = ordinal
                with self.assertRaises(ValueError):
                    self.call()

    def test_unstarted_omission_or_overlap_refuses(self):
        self.unstarted = []
        with self.assertRaisesRegex(ValueError, 'unstarted_inventory'):
            self.call()
        self.unstarted = [dict(self.jobs[0], ordinal=0, input_sha256=core.digest(self.jobs[0]))]
        with self.assertRaisesRegex(ValueError, 'unstarted_inventory'):
            self.call()

    def test_duplicate_job_refuses(self):
        self.jobs.append(copy.deepcopy(self.jobs[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_original_measurement'):
            self.call()


if __name__ == '__main__':
    unittest.main()
