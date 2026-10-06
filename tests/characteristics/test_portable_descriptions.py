import copy
import unittest

from characteristics.coherence.repeat_table import action_relation, build_repeat_table, label_relation
from characteristics.downstream_effects.full_continuations import describe as full


class RepeatDescriptionTests(unittest.TestCase):
    def test_nulls_are_separate_from_agreement(self):
        self.assertEqual(action_relation({'task_success_bool_or_null': None}, {'task_success_bool_or_null': None}), 'both_null')
        self.assertEqual(label_relation({'label_present': False}, {'label_present': False}, 'label_state_or_null'), 'both_missing')

    def test_fixed_frame_pair_weights_and_duplicate_rejection(self):
        rows = []
        for question in range(7):
            for method in range(4):
                for pair in range(28):
                    for repeat in (1, 2):
                        rows.append(dict(actor=f'actor{pair}', actor_model='synthetic', actor_role='fixture', task_id=f'task{pair}', task_code='fixture', family='synthetic', source_cluster_id=f'cluster{pair}', core_sha256='0'*64,
                            question_type=f'q{question}', method=f'm{method}', reader_profile='synthetic', reader_model='synthetic', frame='current_interaction', target_channel='public_text', repeat=repeat, original_assignment_id=f'pair{pair}-r{repeat}', weight_exact_fraction='1/56', ACT_status_raw='synthetic', accepted_action_class='null', accepted_null_cause='synthetic_missing', task_success_bool_or_null=None, label_present=False,
                            label_state_or_null=None, affirmative_assertion_present_bool_or_null=None, relevant_expression_bool_or_null=None))
        result = build_repeat_table(rows)
        self.assertEqual(len(result['pairs']), 784)
        self.assertEqual(len(result['table']), 2100)
        self.assertTrue(all(row['action_relation_counts']['both_null'] == 28 for row in result['coverage']))
        rows[-1] = rows[-2]
        with self.assertRaises(AssertionError):
            build_repeat_table(rows)


class FullContinuationTests(unittest.TestCase):
    def selection(self):
        return [dict(assignment_id=f'{task}-{factor}-{level}', task=f'task{task}', instance_id=f'instance{task}', condition=f'{factor}.L{level}', factor=factor, level=level, continue_full=True, replicate=1, block_id=f'block{task}-{factor}', realization=1, qwen_replica=1, qwen_seed=task*10+level, balanced_schedule_stratum=1, actor_slot_randomization_key=str(task))
                for task in range(6) for factor, levels in [('F07', (0, 4)), ('F08', (0, 3))] for level in levels]

    def test_all_missing_positions_stay_in_frame(self):
        selected = self.selection()
        projected = [full.project(row, {'id': row['assignment_id'], 'status': 'unstarted'}) for row in selected]
        result = full.describe('Qwen', selected, projected)
        self.assertEqual(result['assigned'], 24)
        self.assertTrue(all(row['assigned_difference_identification_bounds'] == [-1, 1] for row in result['native_endpoint_descriptive']))
        with self.assertRaises(ValueError):
            full.describe('Qwen', selected, projected[:-1])
        changed = copy.deepcopy(projected)
        changed[0]['replicate'] += 1
        with self.assertRaises(ValueError):
            full.describe('Qwen', selected, changed)

    def test_unknown_scoped_check_is_not_raw_success(self):
        row = self.selection()[0]
        result = full.project(row, {'id': row['assignment_id'], 'genuine_wait': True, 'resources_releasable': True},
            {'id': row['assignment_id'], 'resources_releasable': True, 'checker': {'scoped_task_native_passed': None, 'raw_selected_native_passed': True}})
        self.assertEqual(result['native_check'], 'unknown')
