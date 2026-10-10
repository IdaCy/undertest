import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from causes import campaign_descriptive_figures as figures


def value(run, label, *, method='self', stage='early', status=None):
    return {'identity': (run, stage, method, 'evaluation', 'presence'),
            'label': label, 'status': status or ('observed' if label is not None else 'final_technical_missing')}


def fixture():
    config = {'methods': ['self'], 'stages': ['early'], 'awareness_types': ['evaluation', 'oversight'],
              'thresholds': ['presence'], 'missingness': [
                  {'status': 'final_technical_missing', 'label': 'technical missing', 'color': '#999999', 'hatch': '..'}],
              'title': 'Synthetic categorical fixture', 'footer': 'Synthetic inputs only',
              'annotate_min_count': 1}
    rows = [
        {'method': 'self', 'stage': 'early', 'awareness_type': 'evaluation', 'threshold': 'presence',
         'assigned': 4, 'observed': 3, 'dispositions': {'observed': 3, 'final_technical_missing': 1},
         'categories': dict(zip(figures.descriptive.CATEGORIES, [1, 2, 0, 0, 0]))},
        {'method': 'self', 'stage': 'early', 'awareness_type': 'oversight', 'threshold': 'presence',
         'assigned': 3, 'observed': 3, 'dispositions': {'observed': 3},
         'categories': dict(zip(figures.descriptive.CATEGORIES, [3, 0, 0, 0, 0]))},
    ]
    return rows, config


class MatrixTests(unittest.TestCase):
    def test_common_observed_matrix_keeps_null_transition_and_assigned_bounds(self):
        left = [value('a', 'yes'), value('b', 'no'), value('c', None)]
        right = [value('a', 'no', method='reader'), value('b', 'yes', method='reader'),
                 value('c', 'yes', method='reader')]
        before = copy.deepcopy((left, right))
        result = figures.pair_summary(left, right)
        self.assertEqual((result['assigned_pairs'], result['complete_pairs']), (3, 2))
        self.assertEqual(result['common_observed_matrix'][0][1], 1)
        self.assertEqual(result['common_observed_matrix'][1][0], 1)
        self.assertEqual(sum(map(sum, result['common_observed_matrix'])), 2)
        self.assertIn({'left': None, 'right': 'yes', 'count': 1}, result['transitions'])
        self.assertEqual(result['right_minus_left_assigned_mean_bounds'], [0, 1 / 3])
        self.assertEqual(result['right_minus_left_observed_pair_mean_descriptive'], 0)
        self.assertEqual((left, right), before)

    def test_shared_unobserved_measurement_has_zero_difference(self):
        missing = [value('a', None), value('b', None)]
        result = figures.pair_summary(missing, copy.deepcopy(missing))
        self.assertEqual(result['same_physical_measurement_pairs'], 2)
        self.assertEqual(result['complete_pairs'], 0)
        self.assertEqual(result['right_minus_left_assigned_mean_bounds'], [0, 0])
        self.assertIsNone(result['right_minus_left_observed_pair_mean_descriptive'])

    def test_context_and_stage_pairing_require_matching_instruments(self):
        early = [value('a', 'yes')]
        end = [value('a', None, stage='end', status='final_actor_capture_unavailable')]
        result = figures.stage_pair_summary(early, end)
        self.assertEqual(result['right_minus_left_assigned_mean_bounds'], [-1, 0])
        for left, right in ((early, [value('b', 'no')]), (early, [])):
            with self.assertRaises(ValueError):
                figures.pair_summary(left, right)
        with self.assertRaisesRegex(ValueError, 'unmatched_stage_instrument'):
            figures.stage_pair_summary(early, [value('a', 'no', method='reader', stage='end')])
        with self.assertRaisesRegex(ValueError, 'pair_matrix_denominators'):
            figures.matrix_summary({'transitions': [{'left': 'yes', 'right': 'no', 'count': 1}],
                                    'complete_pairs': 2, 'assigned_pairs': 1})


class PlotDataTests(unittest.TestCase):
    def test_each_explicit_assigned_population_is_used_without_mutation(self):
        rows, config = fixture()
        before = copy.deepcopy((rows, config))
        prepared = figures.prepare_plot_rows(rows, config)
        self.assertEqual(prepared[0]['percentages'], [25, 50, 0, 0, 0, 25])
        self.assertEqual(prepared[1]['percentages'], [100, 0, 0, 0, 0, 0])
        self.assertEqual((rows, config), before)

    def test_unknown_missingness_and_inconsistent_counts_are_not_dropped(self):
        rows, config = fixture()
        rows[0]['dispositions'] = {'observed': 3, 'pending_reader': 1}
        with self.assertRaisesRegex(ValueError, 'unmapped_plot_disposition'):
            figures.prepare_plot_rows(rows, config)
        config['missingness'].append({'status': 'pending_reader', 'label': 'pending', 'color': '#cccccc'})
        self.assertEqual(figures.prepare_plot_rows(rows, config)[0]['percentages'][-1], 25)
        rows[0]['assigned'] = 5
        with self.assertRaisesRegex(ValueError, 'plot_count_total'):
            figures.prepare_plot_rows(rows, config)

    def test_grid_duplicates_gaps_and_invalid_counts_refuse(self):
        rows, config = fixture()
        with self.assertRaisesRegex(ValueError, 'duplicate_plot_cell'):
            figures.prepare_plot_rows(rows + [copy.deepcopy(rows[0])], config)
        with self.assertRaisesRegex(ValueError, 'plot_grid_membership_changed'):
            figures.prepare_plot_rows(rows[:1], config)
        for invalid in (True, 4.0, -1, 0):
            changed = copy.deepcopy(rows)
            changed[0]['assigned'] = invalid
            with self.assertRaisesRegex(ValueError, 'invalid_plot_counts'):
                figures.prepare_plot_rows(changed, config)

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'optional matplotlib dependency')
    def test_rendered_bars_use_assigned_denominators_and_preserve_existing_files(self):
        from matplotlib.axes import Axes
        rows, config = fixture()
        calls, texts = [], []
        original_bar, original_text = Axes.barh, Axes.text

        def bar(ax, y, width, *args, **kwargs):
            calls.append((y, width, kwargs['left']))
            return original_bar(ax, y, width, *args, **kwargs)

        def text(ax, x, y, content, *args, **kwargs):
            texts.append(str(content))
            return original_text(ax, x, y, content, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(Axes, 'barh', bar), mock.patch.object(Axes, 'text', text):
                paths = figures.plot_distributions(rows, Path(temporary), config)
            self.assertTrue(paths['png'].read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn('<svg', paths['svg'].read_text())
            self.assertEqual([width for _, width, _ in calls[:6]], [25, 50, 0, 0, 0, 25])
            self.assertEqual(calls[5][2], 75)
            self.assertIn('3/4', texts)
            self.assertIn('3/3', texts)
            before = {key: path.read_bytes() for key, path in paths.items()}
            with self.assertRaises(FileExistsError):
                figures.plot_distributions(rows, Path(temporary), config)
            self.assertEqual({key: path.read_bytes() for key, path in paths.items()}, before)


if __name__ == '__main__':
    unittest.main()
