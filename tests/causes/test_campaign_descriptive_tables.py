import copy
import json
import unittest

from causes.campaign_descriptive import CATEGORIES, READER_NAMES, THRESHOLDS, TYPES
from causes.campaign_descriptive_tables import compose_tables


def fixture():
    jobs, records, variants = {}, {}, {}
    reader_labels = {value: key for key, value in READER_NAMES.items()}
    labels = dict(zip(('a0', 'a1', 'a2', 'b0', 'b1', 'b2'), (*CATEGORIES, None)))
    for task in ('a', 'b'):
        variants[task] = {level: {'eligible': True} for level in ('natural', 'style_a', 'style_b')}
        variants[task]['alias'] = {'eligible': True, 'alias_of': 'natural'}
        for number, level in enumerate(('natural', 'style_a', 'style_b')):
            rid = task + str(number)
            full = number == 0
            instruments = ['evaluation_' + threshold for threshold in THRESHOLDS]
            if full:
                instruments += [kind + '_presence' for kind in TYPES if kind != 'evaluation']
            allocation = dict(run_id=rid, instruments=instruments,
                              reader_stages=['early', 'end'] if full else ['early'],
                              reader_thresholds=list(THRESHOLDS), continue_to_end=full,
                              descriptive_first_rep=True, repetition=0, canonical_variant=level)
            jobs[rid] = dict(allocation=allocation, spec={'task_id': task})
            stages = allocation['reader_stages']
            label = labels[rid]
            records[rid] = dict(
                model='model', run_id=rid, task_id=task, allocation=copy.deepcopy(allocation),
                context_sources={stage: {'fixture': True} for stage in stages},
                self={stage: {instrument: {'label': label} for instrument in instruments} for stage in stages},
                reader={stage: {'instruments': {
                    threshold: {'labels': {kind: reader_labels[label] for kind in TYPES} if label else {}}
                    for threshold in THRESHOLDS}} for stage in stages},
                reader_dispositions={stage: 'complete' for stage in stages})
    return dict(model='model', jobs=jobs, records=records,
                rosters=dict(threshold_early=sorted(jobs), threshold_end=['a0', 'b0'], awareness_common=['a0', 'b0']),
                condition_families={'style': dict(tasks=['b', 'a'], control='natural',
                                                   levels=['natural', 'style_a', 'style_b', 'alias'])},
                variant_bindings=variants)


class DescriptiveTableTests(unittest.TestCase):
    def test_all_categories_and_explicit_denominators(self):
        result = compose_tables(**fixture())
        self.assertEqual(set(result), {'threshold_pairs', 'awareness_types', 'self_reader_pairs', 'dose_levels', 'ROSTERS'})
        early = result['threshold_pairs']['model']['early']['self']['distributions']['presence']
        self.assertEqual(early['assigned'], 6)
        self.assertEqual(early['observed'], 5)
        self.assertEqual(early['categories'], dict.fromkeys(CATEGORIES, 1))
        self.assertEqual(early['dispositions'], {'observed': 5, 'final_technical_missing': 1})
        self.assertEqual(early['observed_yes_fraction_descriptive'], 1 / 5)
        self.assertEqual(early['assigned_yes_fraction_bounds'], [1 / 6, 2 / 6])
        end = result['threshold_pairs']['model']['end']['self']['distributions']['presence']
        self.assertEqual(end['assigned'], 2)
        common = result['awareness_types']['model']['early']['common_self']['presence']
        self.assertEqual(len(common['distributions']), 7)
        self.assertEqual(len(common['paired_awareness_types']), 21)
        self.assertTrue(all(row['assigned'] == 2 for row in common['distributions'].values()))
        self.assertEqual(result['self_reader_pairs']['model']['early']['evaluation_all_selected']['presence']['assigned_pairs'], 6)

    def test_all_missingness_dispositions_are_retained(self):
        args = fixture()
        del args['records']['a1']; args['quality_unavailable'] = ['a1']
        del args['records']['a2']
        args['records']['b0']['context_sources'] = {}
        args['records']['b1']['self']['early'] = {}
        args['records']['b1']['reader']['early'] = {}
        args['records']['b1']['reader_dispositions']['early'] = 'pending_reader'
        result = compose_tables(**args)['threshold_pairs']['model']['early']
        expected = {'observed': 1, 'final_quality_unavailable': 1, 'pending_actor_final_record': 1,
                    'final_actor_capture_unavailable': 1, 'final_technical_missing': 2}
        self.assertEqual(result['self']['distributions']['presence']['dispositions'], expected)
        expected['final_technical_missing'] = 1; expected['pending_reader'] = 1
        self.assertEqual(result['reader']['distributions']['presence']['dispositions'], expected)
        for method in ('self', 'reader'):
            self.assertEqual(result[method]['distributions']['presence']['assigned_yes_fraction_bounds'], [1 / 6, 1])

    def test_aliases_retain_zero_difference_even_when_unobserved(self):
        args = fixture(); del args['records']['a0']; del args['records']['b0']
        result = compose_tables(**args)
        family = result['dose_levels']['model']['style']
        alias = family['level_minus_control']['alias']
        self.assertEqual(alias['assigned_pairs'], 2)
        self.assertEqual(alias['complete_pairs'], 0)
        self.assertEqual(alias['same_physical_measurement_pairs'], 2)
        self.assertEqual(alias['right_minus_left_assigned_mean_bounds'], [0, 0])
        self.assertIsNone(alias['right_minus_left_observed_pair_mean_descriptive'])
        self.assertEqual(family['shared_physical_draws_across_levels'], 2)
        roster = result['ROSTERS']['model']['dose']['style']
        self.assertEqual(roster['tasks_in_order'], ['b', 'a'])
        self.assertEqual(roster['initial_draws_by_level']['alias'], ['b0', 'a0'])

    def test_conditions_are_matched_to_baseline_without_ordinal_constraint(self):
        args = fixture(); result = compose_tables(**args)['dose_levels']['model']['style']
        pair = result['level_minus_control']['style_a']
        self.assertEqual(pair['complete_pairs'], 2)
        self.assertEqual(pair['right_minus_left_assigned_mean_bounds'], [-.5, -.5])
        self.assertEqual(pair['right_minus_left_observed_pair_mean_descriptive'], -.5)
        args['condition_families']['style']['levels'] = ['style_b', 'alias', 'style_a']
        reordered = compose_tables(**args)['dose_levels']['model']['style']
        self.assertEqual(reordered, result)
        self.assertEqual(list(reordered['distributions']), ['natural', 'style_b', 'alias', 'style_a'])

    def test_thresholds_and_methods_are_not_forced_to_agree(self):
        args = fixture(); row = args['records']['a0']
        row['self']['early']['evaluation_presence']['label'] = 'no'
        row['self']['early']['evaluation_strongly_favors']['label'] = 'yes'
        result = compose_tables(**args)
        pair = result['threshold_pairs']['model']['early']['self']['paired_thresholds']['presence__to__strongly_favors']
        self.assertEqual(pair['right_minus_left_observed_pair_mean_descriptive'], 1 / 5)
        self.assertIn({'left': 'no', 'right': 'yes', 'count': 1}, pair['transitions'])
        method = result['self_reader_pairs']['model']['early']['evaluation_all_selected']['presence']
        self.assertEqual(method['right_minus_left_observed_pair_mean_descriptive'], 1 / 5)

    def test_result_is_deterministic_and_does_not_mutate_or_alias_inputs(self):
        args = fixture(); args['rosters']['threshold_early'].reverse()
        before = copy.deepcopy(args)
        first, second = compose_tables(**args), compose_tables(**args)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(args, before)
        self.assertEqual(first['ROSTERS']['model']['threshold_early'], before['rosters']['threshold_early'])
        first['ROSTERS']['model']['threshold_early'].append('not-an-input')
        first['ROSTERS']['model']['dose']['style']['tasks_in_order'].clear()
        self.assertEqual(args, before)

    def test_roster_cannot_drop_pending_assignments_or_duplicate_members(self):
        for change in ('drop', 'duplicate', 'unknown', 'unordered'):
            with self.subTest(change=change):
                args = fixture(); del args['records']['a2']
                if change == 'drop': args['rosters']['threshold_early'].remove('a2')
                elif change == 'duplicate': args['rosters']['threshold_early'].append('a0')
                elif change == 'unknown': args['rosters']['threshold_early'][-1] = 'unknown'
                else: args['rosters']['threshold_early'] = set(args['rosters']['threshold_early'])
                with self.assertRaises(ValueError): compose_tables(**args)

    def test_quality_and_record_identity_conflicts_refuse(self):
        for change in ('observed_quality', 'unknown_quality', 'unknown_record', 'model', 'run', 'task', 'typed_allocation'):
            with self.subTest(change=change):
                args = fixture()
                if change == 'observed_quality': args['quality_unavailable'] = ['a0']
                elif change == 'unknown_quality': args['quality_unavailable'] = ['unknown']
                elif change == 'unknown_record': args['records']['unknown'] = args['records']['a0']
                elif change == 'model': args['records']['a0']['model'] = 'other'
                elif change == 'run': args['records']['a0']['run_id'] = 'other'
                elif change == 'task': args['records']['a0']['task_id'] = 'other'
                else: args['records']['a0']['allocation']['repetition'] = 0.0
                with self.assertRaises(ValueError): compose_tables(**args)

    def test_condition_eligibility_alias_and_initial_draw_guards(self):
        for change in ('ineligible', 'unknown_alias', 'alias_chain', 'missing_draw', 'duplicate_draw', 'not_initial', 'duplicate_task'):
            with self.subTest(change=change):
                args = fixture()
                if change == 'ineligible': args['variant_bindings']['a']['style_a']['eligible'] = False
                elif change == 'unknown_alias': args['variant_bindings']['a']['alias']['alias_of'] = 'unknown'
                elif change == 'alias_chain': args['variant_bindings']['a']['natural']['alias_of'] = 'alias'
                elif change == 'missing_draw': args['jobs']['a1']['allocation']['descriptive_first_rep'] = False
                elif change == 'duplicate_draw': args['jobs']['a1']['allocation']['canonical_variant'] = 'natural'
                elif change == 'not_initial': args['jobs']['a1']['allocation']['repetition'] = 1
                else: args['condition_families']['style']['tasks'] = ['a', 'a']
                for rid in args['records']:
                    args['records'][rid]['allocation'] = copy.deepcopy(args['jobs'][rid]['allocation'])
                with self.assertRaises(ValueError): compose_tables(**args)

    def test_unassigned_instrument_and_unknown_semantic_label_refuse(self):
        args = fixture()
        args['jobs']['a1']['allocation']['instruments'].remove('evaluation_strongly_favors')
        args['records']['a1']['allocation'] = copy.deepcopy(args['jobs']['a1']['allocation'])
        with self.assertRaisesRegex(ValueError, 'self_instrument_not_assigned'): compose_tables(**args)
        args = fixture(); args['records']['a1']['self']['early']['evaluation_presence']['label'] = 'maybe'
        with self.assertRaisesRegex(ValueError, 'unknown_semantic_label'): compose_tables(**args)


if __name__ == '__main__':
    unittest.main()
