import copy
import unittest

from causes.campaign_prompt_partition import partition_prompt_queue


def fixture():
    jobs, systems = [], {}
    for pair, arms in [('p1', ['a', 'b']), ('p2', ['b', 'a']), ('p3', ['a', 'b'])]:
        for arm in arms:
            run_id = pair + '_' + arm
            jobs.append({'spec': {'task_id': pair}, 'variant': {'id': 'B00L00'},
                         'allocation': {'run_id': run_id, 'seed': 7,
                                        'continue_to_end': pair == 'p1',
                                        'paired_baseline': {'original_run_id': 'old_' + pair},
                                        'system_prompt_intervention': {
                                            'namespace': 'study', 'pair_id': pair,
                                            'arm': {'id': arm, 'text': arm},
                                            'same_seed_intentional': True}}})
            systems[run_id] = 'system ' + arm
    return jobs, systems, {'p1': 'h1', 'p2': 'h2', 'p3': 'h1'}


class PartitionTests(unittest.TestCase):
    def call(self, jobs, systems, hosts, arms=('a', 'b')):
        return partition_prompt_queue(jobs, systems, hosts, expected_arm_ids=arms)

    def test_disjoint_cover_preserves_order_and_nested_objects(self):
        jobs, systems, hosts = fixture()
        before = copy.deepcopy((jobs, systems, hosts))
        result = self.call(jobs, systems, hosts)
        self.assertEqual([j['allocation']['run_id'] for j in result['h1']['jobs']],
                         ['p1_a', 'p1_b', 'p3_a', 'p3_b'])
        self.assertEqual([j['allocation']['run_id'] for j in result['h2']['jobs']], ['p2_b', 'p2_a'])
        self.assertEqual(result['h1']['pair_ids'], ['p1', 'p3'])
        actual = {j['allocation']['run_id']: j for shard in result.values() for j in shard['jobs']}
        self.assertEqual(actual, {j['allocation']['run_id']: j for j in jobs})
        self.assertEqual({k: v for shard in result.values() for k, v in shard['system_prompts'].items()}, systems)
        result['h1']['jobs'][0]['spec']['task_id'] = 'changed'
        self.assertEqual((jobs, systems, hosts), before)

    def test_pair_cannot_lose_or_repeat_arm(self):
        jobs, systems, hosts = fixture()
        del systems[jobs.pop(0)['allocation']['run_id']]
        with self.assertRaisesRegex(ValueError, 'incomplete_pair'):
            self.call(jobs, systems, hosts)
        jobs, systems, hosts = fixture()
        jobs[1]['allocation']['system_prompt_intervention']['arm'] = {'id': 'a', 'text': 'a'}
        with self.assertRaisesRegex(ValueError, 'duplicate_pair_arm'):
            self.call(jobs, systems, hosts)

    def test_pair_invariants_include_seed_full_flag_task_and_baseline(self):
        for field in ('seed', 'continue_to_end', 'paired_baseline', 'spec'):
            with self.subTest(field=field):
                jobs, systems, hosts = fixture()
                if field == 'spec':
                    jobs[1]['spec']['task_id'] = 'another_task'
                elif field == 'paired_baseline':
                    jobs[1]['allocation'][field]['original_run_id'] = 'another_original'
                elif field == 'seed':
                    jobs[1]['allocation'][field] = 8
                else:
                    jobs[1]['allocation'][field] = False
                with self.assertRaisesRegex(ValueError, 'paired_job_mismatch'):
                    self.call(jobs, systems, hosts)

    def test_host_and_system_maps_are_exact(self):
        for mapping, change in [('hosts', 'missing'), ('hosts', 'extra'),
                                ('systems', 'missing'), ('systems', 'extra')]:
            with self.subTest(mapping=mapping, change=change):
                jobs, systems, hosts = fixture()
                target = hosts if mapping == 'hosts' else systems
                if change == 'missing':
                    del target[next(iter(target))]
                else:
                    target['unknown'] = 'value'
                with self.assertRaises(ValueError):
                    self.call(jobs, systems, hosts)

    def test_duplicate_run_unknown_arm_and_namespace_rejected(self):
        for change in ('run', 'arm', 'namespace'):
            with self.subTest(change=change):
                jobs, systems, hosts = fixture()
                allocation = jobs[-1]['allocation']
                if change == 'run':
                    allocation['run_id'] = jobs[0]['allocation']['run_id']
                elif change == 'arm':
                    allocation['system_prompt_intervention']['arm']['id'] = 'unknown'
                else:
                    allocation['system_prompt_intervention']['namespace'] = 'another'
                with self.assertRaises(ValueError):
                    self.call(jobs, systems, hosts)

    def test_arm_definition_drift_rejected(self):
        jobs, systems, hosts = fixture()
        jobs[-1]['allocation']['system_prompt_intervention']['arm']['text'] = 'different'
        with self.assertRaisesRegex(ValueError, 'inconsistent_arm_definition'):
            self.call(jobs, systems, hosts)

    def test_json_scalar_types_are_part_of_pair_and_arm_identity(self):
        for change in ('seed', 'nested_provenance', 'arm'):
            with self.subTest(change=change):
                jobs, systems, hosts = fixture()
                if change == 'seed':
                    jobs[1]['allocation']['seed'] = 7.0
                elif change == 'nested_provenance':
                    jobs[0]['allocation']['paired_baseline']['version'] = 1
                    jobs[1]['allocation']['paired_baseline']['version'] = True
                else:
                    jobs[0]['allocation']['system_prompt_intervention']['arm']['primary'] = True
                    jobs[3]['allocation']['system_prompt_intervention']['arm']['primary'] = 1
                with self.assertRaises(ValueError):
                    self.call(jobs, systems, hosts)

    def test_invalid_inputs_rejected(self):
        for arms in ([], ['a', 'a'], ['a', None], 'ab'):
            with self.subTest(arms=arms), self.assertRaises(ValueError):
                self.call(*fixture(), arms=arms)
        jobs, systems, hosts = fixture()
        jobs[0]['allocation']['continue_to_end'] = 1
        with self.assertRaisesRegex(ValueError, 'invalid_full_flag'):
            self.call(jobs, systems, hosts)
        with self.assertRaises(ValueError):
            self.call([], {}, {})


if __name__ == '__main__':
    unittest.main()
