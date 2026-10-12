import copy
import unittest

from causes.campaign_prompt_partition import partition_unclaimed_job_groups


def fixture():
    runs = ['a0', 'b0', 'a1', 'c0', 'b1', 'b2']
    jobs = [{'allocation': {'run_id': run, 'seed': i, 'instruments': ['one', 'two']},
             'spec': {'task_id': run[0]}, 'variant': {'id': run[1:]}}
            for i, run in enumerate(runs)]
    systems = {run: 'system ' + run for run in runs}
    groups = {'a': ['a0', 'a1'], 'b': ['b0', 'b1', 'b2'], 'c': ['c0']}
    return jobs, systems, groups, {'a': 'old', 'b': 'new', 'c': 'old'}


class GroupPartitionTests(unittest.TestCase):
    def call(self, jobs, systems, groups, hosts, claimed=()):
        return partition_unclaimed_job_groups(jobs, systems, groups, hosts, claimed,
                                              previous_host='old')

    def test_partial_group_stays_and_untouched_group_moves_in_original_order(self):
        args = fixture()
        before = copy.deepcopy(args)
        result = self.call(*args, claimed=['a0', 'c0'])
        self.assertEqual(result['old']['jobs'], [args[0][2]])
        self.assertEqual(result['new']['jobs'], [args[0][i] for i in (1, 4, 5)])
        self.assertEqual(result['old']['group_ids'], ['a'])
        self.assertEqual(result['new']['group_ids'], ['b'])
        self.assertEqual(result['new']['system_prompts'],
                         {run: args[1][run] for run in ('b0', 'b1', 'b2')})
        remaining = [job['allocation']['run_id'] for shard in result.values()
                     for job in shard['jobs']]
        self.assertEqual(set(remaining), set(args[1]) - {'a0', 'c0'})
        self.assertEqual(len(remaining), len(set(remaining)))
        result['new']['jobs'][0]['allocation']['instruments'].append('changed')
        self.assertEqual(args, before)

    def test_started_group_cannot_move_including_failed_or_completed_claims(self):
        for claims in (['b0'], ['b0', 'b1', 'b2']):
            with self.subTest(claims=claims), self.assertRaisesRegex(
                    ValueError, 'started_group_host_changed'):
                self.call(*fixture(), claimed=claims)

    def test_every_claim_is_excluded_and_an_empty_continuation_is_explicit(self):
        jobs, systems, groups, hosts = fixture()
        hosts = {key: 'old' for key in hosts}
        self.assertEqual(self.call(jobs, systems, groups, hosts, list(systems)),
                         {'old': {'jobs': [], 'system_prompts': {}, 'group_ids': []}})

    def test_no_claims_preserves_all_jobs_without_reordering_interleaved_groups(self):
        jobs, systems, groups, hosts = fixture()
        hosts = {key: 'old' for key in hosts}
        result = self.call(jobs, systems, groups, hosts)
        self.assertEqual(result['old'],
                         {'jobs': jobs, 'system_prompts': systems, 'group_ids': ['a', 'b', 'c']})

    def test_foreign_overlapping_missing_and_reordered_members_are_refused(self):
        for change in ('foreign', 'overlap', 'missing', 'order', 'duplicate'):
            jobs, systems, groups, hosts = fixture()
            if change == 'foreign':
                groups['a'] = ['a0', 'outside']
            elif change == 'overlap':
                groups['a'].append('b0')
            elif change == 'missing':
                groups['a'].remove('a1')
            elif change == 'order':
                groups['a'].reverse()
            else:
                groups['a'].append('a0')
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call(jobs, systems, groups, hosts)

    def test_group_and_prompt_maps_must_cover_exactly_the_original_jobs(self):
        for mapping in ('hosts', 'prompts'):
            for change in ('extra', 'missing'):
                jobs, systems, groups, hosts = fixture()
                target = hosts if mapping == 'hosts' else systems
                if change == 'extra':
                    target['unknown'] = 'new'
                else:
                    del target[next(iter(target))]
                with self.subTest(mapping=mapping, change=change), self.assertRaises(ValueError):
                    self.call(jobs, systems, groups, hosts)

    def test_duplicate_jobs_and_malformed_claims_are_refused(self):
        for claims in (['unknown'], ['a0', 'a0'], [''], [None], [[]], 'a0', {'a0'}):
            with self.subTest(claims=claims), self.assertRaises(ValueError):
                self.call(*fixture(), claimed=claims)
        jobs, systems, groups, hosts = fixture()
        jobs.append(copy.deepcopy(jobs[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_or_invalid_run'):
            self.call(jobs, systems, groups, hosts)

    def test_invalid_group_host_job_and_prompt_values_are_refused(self):
        for change in ('group', 'host', 'empty_members', 'members_type', 'member_type',
                       'job', 'run', 'prompt'):
            jobs, systems, groups, hosts = fixture()
            if change == 'group':
                groups[''] = groups.pop('a')
                hosts[''] = hosts.pop('a')
            elif change == 'host':
                hosts['a'] = []
            elif change == 'empty_members':
                groups['a'] = []
            elif change == 'members_type':
                groups['a'] = 'a0'
            elif change == 'member_type':
                groups['a'] = [[]]
            elif change == 'job':
                jobs[0] = None
            elif change == 'run':
                jobs[0]['allocation']['run_id'] = ''
            else:
                systems['a0'] = ''
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call(jobs, systems, groups, hosts)

    def test_original_group_integrity_is_checked_even_if_all_jobs_were_claimed(self):
        jobs, systems, groups, hosts = fixture()
        hosts = {key: 'old' for key in hosts}
        groups['a'].reverse()
        with self.assertRaisesRegex(ValueError, 'group_order_changed'):
            self.call(jobs, systems, groups, hosts, list(systems))


if __name__ == '__main__':
    unittest.main()
