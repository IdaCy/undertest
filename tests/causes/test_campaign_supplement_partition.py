import copy
import unittest

from causes.campaign_supplement_partition import (
    hashed_context_assignment, partition_supplement_jobs)


QUESTIONS = ('strong', 'lean')


def fixture(count=12):
    jobs = []
    for index in range(count):
        run = 'r' + str(index)
        for question in QUESTIONS:
            jobs.append(dict(family='study', model='model', run_id=run, stage='early',
                             question_id=question,
                             measurement_id='/'.join(('study', 'model', run, 'early', question)),
                             context={'path': '/' + run, 'bytes': 3, 'sha256': 'a' * 64},
                             context_canonical_sha256='b' * 64,
                             frozen_config={'sha256': 'c' * 64}, original_refs={'run': run},
                             native_job_sha256='d' * 64, original_seed=index,
                             awareness_type='evaluation', question_canonical_sha256=question,
                             threshold=question, seed=index + 100, request_body_sha256='e' * 64,
                             request_file_sha256='f' * 64, request_body_bytes=300,
                             extra={'literal': [True, index]}))
    return jobs


def assign(jobs, **kwargs):
    return hashed_context_assignment(jobs, {'mi': 6, 'ny': 3, 'texas': 3},
                                     salt='fixed-v1', question_ids=QUESTIONS, **kwargs)


class SupplementPartitionTests(unittest.TestCase):
    def test_exact_cover_original_order_pairing_and_no_mutation(self):
        jobs = fixture(); before = copy.deepcopy(jobs)
        owners = assign(jobs)
        parts = partition_supplement_jobs(jobs, owners, question_ids=QUESTIONS)
        self.assertEqual({key: len(value['context_ids']) for key, value in parts.items()},
                         {'mi': 6, 'ny': 3, 'texas': 3})
        union = [job['measurement_id'] for part in parts.values() for job in part['jobs']]
        self.assertEqual(len(union), len(set(union)))
        self.assertEqual(set(union), {job['measurement_id'] for job in jobs})
        for owner, part in parts.items():
            selected = set(part['context_ids'])
            self.assertEqual(part['jobs'], [job for job in jobs
                             if job['measurement_id'].rsplit('/', 1)[0] in selected])
            for context in selected:
                self.assertEqual([job['question_id'] for job in part['jobs']
                                  if job['measurement_id'].rsplit('/', 1)[0] == context],
                                 list(QUESTIONS))
        parts['mi']['jobs'][0]['extra']['literal'].append('changed')
        self.assertEqual(jobs, before)

    def test_hash_is_identity_only_and_dictionary_order_independent(self):
        jobs = fixture(); altered = copy.deepcopy(jobs)
        for job in altered:
            job['extra']['unused_result'] = 'different'
            job['request_body_sha256'] = '9' * 64
        self.assertEqual(assign(jobs), assign(altered))
        self.assertEqual(assign(jobs), hashed_context_assignment(
            jobs, {'texas': 3, 'ny': 3, 'mi': 6}, salt='fixed-v1', question_ids=QUESTIONS))
        reversed_groups = sum([jobs[i:i + 2] for i in range(len(jobs)-2, -1, -2)], [])
        self.assertEqual(assign(jobs), assign(reversed_groups))

    def test_complete_unstarted_reservation_can_change_owner_without_ids(self):
        jobs = fixture(); owners = assign(jobs)
        moved = {context: 'ny' if owner == 'texas' else owner for context, owner in owners.items()}
        parts = partition_supplement_jobs(jobs, moved, question_ids=QUESTIONS)
        self.assertEqual(len(parts['ny']['context_ids']), 6)
        self.assertEqual({job['measurement_id']: job for part in parts.values() for job in part['jobs']},
                         {job['measurement_id']: job for job in jobs})

    def test_any_attempt_or_ambiguity_prevents_reassignment(self):
        jobs = fixture(); owners = assign(jobs)
        for key in ('attempted_ids', 'ambiguous_ids'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'attempted_or_ambiguous'):
                partition_supplement_jobs(jobs, owners, question_ids=QUESTIONS,
                                          **{key: [jobs[1]['measurement_id']]})
            with self.assertRaisesRegex(ValueError, 'attempted_or_ambiguous'):
                assign(jobs, **{key: [jobs[1]['measurement_id']]})

    def test_duplicate_or_changed_id_refused(self):
        for change in ('duplicate', 'identity', 'component'):
            jobs = fixture()
            if change == 'duplicate': jobs.append(copy.deepcopy(jobs[0]))
            elif change == 'identity': jobs[0]['measurement_id'] += '_changed'
            else: jobs[0]['run_id'] = '../escape'
            with self.subTest(change=change), self.assertRaises(ValueError): assign(jobs)

    def test_missing_reordered_or_foreign_questions_refused(self):
        for change in ('missing', 'order', 'foreign'):
            jobs = fixture()
            if change == 'missing': jobs.pop()
            elif change == 'order': jobs[:2] = jobs[1::-1]
            else:
                jobs[0]['question_id'] = 'other'
                jobs[0]['measurement_id'] = 'study/model/r0/early/other'
            with self.subTest(change=change), self.assertRaises(ValueError): assign(jobs)

    def test_context_binding_and_scalar_types_are_exact(self):
        for key in ('context', 'frozen_config', 'original_refs', 'native_job_sha256',
                    'original_seed', 'context_canonical_sha256', 'awareness_type'):
            jobs = fixture(); jobs[1][key] = False
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'context_binding_changed'):
                assign(jobs)

    def test_mixed_namespace_and_question_definition_refused(self):
        for key in ('family', 'model', 'stage', 'question_canonical_sha256', 'threshold'):
            jobs = fixture(); jobs[-1][key] = 'end' if key == 'stage' else 'changed'
            if key in ('family', 'model', 'stage'):
                jobs[-1]['measurement_id'] = '/'.join(jobs[-1][x] for x in
                    ('family', 'model', 'run_id', 'stage', 'question_id'))
            with self.subTest(key=key), self.assertRaises(ValueError): assign(jobs)

    def test_exact_ownership_mapping_required(self):
        jobs = fixture(); owners = assign(jobs)
        for change in ('missing', 'extra', 'invalid'):
            altered = dict(owners)
            if change == 'missing': altered.pop(next(iter(altered)))
            elif change == 'extra': altered['unknown'] = 'mi'
            else: altered[next(iter(altered))] = '../escape'
            with self.subTest(change=change), self.assertRaises(ValueError):
                partition_supplement_jobs(jobs, altered, question_ids=QUESTIONS)

    def test_invalid_counts_salt_and_attempt_metadata_refused(self):
        jobs = fixture()
        for counts in ({'mi': 11}, {'mi': True, 'ny': 11}, {'mi': -1, 'ny': 13}, {'mi': 12.0}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                hashed_context_assignment(jobs, counts, salt='fixed', question_ids=QUESTIONS)
        with self.assertRaises(ValueError):
            hashed_context_assignment(jobs, {'mi': 12}, salt='', question_ids=QUESTIONS)
        with self.assertRaises(ValueError): assign(jobs, attempted_ids=['same', 'same'])
        with self.assertRaises(ValueError): assign(jobs, ambiguous_ids=[False])


if __name__ == '__main__':
    unittest.main()
