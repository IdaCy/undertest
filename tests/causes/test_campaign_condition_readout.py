import copy
import unittest

from causes.campaign_condition_readout import compose_condition_readout, distribution_plot_rows


def distribution(yes=1, observed=2, assigned=3):
    return {'assigned': assigned, 'observed': observed,
            'categories': {'yes': yes, 'no': observed - yes},
            'dispositions': {'observed': observed, 'pending': assigned - observed},
            'observed_yes_fraction_descriptive': yes / observed,
            'assigned_yes_fraction_bounds': [yes / assigned, (yes + assigned - observed) / assigned]}


def paired(shared=0):
    return {'assigned_pairs': 3, 'complete_pairs': 1, 'same_physical_measurement_pairs': shared,
            'right_minus_left_observed_pair_mean_descriptive': 1.0,
            'right_minus_left_assigned_mean_bounds': [-1 / 3, 2 / 3],
            'transitions': [{'left': 'no', 'right': 'yes', 'count': 1},
                            {'left': None, 'right': None, 'count': 2}],
            'dispositions': [{'left': 'observed', 'right': 'observed', 'count': 1},
                             {'left': 'pending', 'right': 'pending', 'count': 2}]}


def fixture():
    families = {'direct': {'control': 'plain', 'levels': ['plain', 'cue'], 'tasks': ['c', 'a', 'b']},
                'carrier': {'control': 'carrier', 'levels': ['carrier', 'cue'], 'tasks': ['b', 'c', 'a']}}
    declared = {
        'direct': {'control': 'plain', 'task_count': 3,
                   'distributions': {'plain': distribution(0), 'cue': distribution()},
                   'level_minus_control': {'cue': paired()}},
        'carrier': {'control': 'carrier', 'task_count': 3,
                    'distributions': {'carrier': distribution(), 'cue': distribution(2)},
                    'level_minus_control': {'cue': paired(1)}},
    }
    supplement = {'carrier': {'control': 'plain', 'task_count': 3,
                              'distributions': {'plain': distribution(0), **copy.deepcopy(declared['carrier']['distributions'])},
                              'level_minus_control': {'carrier': paired(1), 'cue': paired()}}}
    labels = {'direct': {'family': 'Declared plain baseline', 'levels': {'plain': 'Original task', 'cue': 'Explicit cue'}},
              'carrier': {'family': 'Matched carrier', 'levels': {'plain': 'Original task', 'carrier': 'Carrier only', 'cue': 'Explicit cue'}}}
    return declared, supplement, families, labels


class ConditionReadoutTests(unittest.TestCase):
    def compose(self, values):
        return compose_condition_readout(*values, baseline='plain')

    def test_distinct_controls_and_untabulated_reference(self):
        original = fixture()
        view = self.compose(original)
        direct, carrier = view['families']
        self.assertTrue(direct['references_identical'])
        self.assertEqual(direct['levels'][1]['declared_comparison'], direct['levels'][1]['natural_comparison'])
        self.assertEqual(carrier['tasks_in_order'], ['b', 'c', 'a'])
        self.assertEqual([r['condition'] for r in carrier['levels']], ['plain', 'carrier', 'cue'])
        self.assertEqual(carrier['levels'][0]['declared_comparison']['status'], 'not_tabulated')
        self.assertEqual(carrier['levels'][0]['natural_comparison']['status'], 'reference')
        self.assertIsNone(carrier['levels'][1]['declared_comparison']['value'])
        self.assertEqual(carrier['levels'][1]['natural_comparison']['value'], original[1]['carrier']['level_minus_control']['carrier'])

    def test_missingness_shared_pairs_and_deep_copy(self):
        values = fixture()
        before = copy.deepcopy(values)
        view = self.compose(values)
        row = view['families'][1]['levels'][1]
        self.assertEqual(row['distribution']['dispositions']['pending'], 1)
        self.assertEqual(row['natural_comparison']['value']['same_physical_measurement_pairs'], 1)
        row['natural_comparison']['value']['transitions'][0]['count'] = 99
        self.assertEqual(values, before)

    def test_plot_adapter_copies_only_selected_family(self):
        view = self.compose(fixture())['families'][1]
        rows = distribution_plot_rows(view, method='self', stage='early', threshold='presence')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['awareness_type'], 'carrier')
        self.assertEqual(rows[1]['dispositions'], view['levels'][1]['distribution']['dispositions'])
        rows[1]['categories']['yes'] = 99
        self.assertEqual(view['levels'][1]['distribution']['categories']['yes'], 1)

    def test_refuses_changed_original_distribution(self):
        values = fixture()
        values[1]['carrier']['distributions']['carrier'] = distribution(0)
        with self.assertRaisesRegex(ValueError, 'original_distribution_changed'):
            self.compose(values)

    def test_plot_adapter_supplied_labels_override_reserved_extra_keys(self):
        view = self.compose(fixture())['families'][1]
        view['levels'][0]['distribution'].update(method='reader', stage='end',
                                               threshold='strongly_favors', awareness_type='different')
        row = distribution_plot_rows(view, method='self', stage='early', threshold='presence')[0]
        self.assertEqual({key: row[key] for key in ('method', 'stage', 'threshold', 'awareness_type')},
                         {'method': 'self', 'stage': 'early', 'threshold': 'presence', 'awareness_type': 'plain'})

    def test_refuses_wrong_control_or_denominator(self):
        for section, value in [('control', 'other'), ('task_count', 2)]:
            values = fixture()
            values[1]['carrier'][section] = value
            with self.assertRaisesRegex(ValueError, 'natural_control_or_roster_mismatch'):
                self.compose(values)

    def test_refuses_incomplete_or_extra_supplement(self):
        values = fixture()
        values[1].clear()
        with self.assertRaisesRegex(ValueError, 'supplement_family_membership_mismatch'):
            self.compose(values)
        values = fixture()
        values[1]['direct'] = values[0]['direct']
        with self.assertRaisesRegex(ValueError, 'supplement_family_membership_mismatch'):
            self.compose(values)

    def test_refuses_dropped_level_or_label(self):
        values = fixture()
        values[0]['carrier']['level_minus_control'].clear()
        with self.assertRaisesRegex(ValueError, 'declared_level_membership_mismatch'):
            self.compose(values)
        values = fixture()
        del values[3]['carrier']['levels']['plain']
        with self.assertRaisesRegex(ValueError, 'display_label_membership_mismatch'):
            self.compose(values)

    def test_refuses_duplicate_task_or_changed_count_total(self):
        values = fixture()
        values[2]['direct']['tasks'] = ['a', 'a', 'b']
        with self.assertRaisesRegex(ValueError, 'invalid_task_roster'):
            self.compose(values)
        values = fixture()
        values[0]['direct']['distributions']['plain']['dispositions']['pending'] = 2
        with self.assertRaisesRegex(ValueError, 'distribution_count_total'):
            self.compose(values)

    def test_refuses_invalid_pair_denominator_without_reestimating(self):
        values = fixture()
        values[1]['carrier']['level_minus_control']['carrier']['assigned_pairs'] = 2
        with self.assertRaisesRegex(ValueError, 'paired_assignment_mismatch'):
            self.compose(values)

    def test_refuses_boolean_counts(self):
        values = fixture()
        values[0]['direct']['distributions']['plain']['observed'] = True
        with self.assertRaisesRegex(ValueError, 'invalid_observed_count'):
            self.compose(values)


if __name__ == '__main__':
    unittest.main()
