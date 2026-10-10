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


def matrix_fixture():
    summary = {
        'left': 'presence', 'right': 'strongly_favors',
        'matrix_labels': list(figures.descriptive.CATEGORIES),
        'matrix_direction': 'rows = left instrument; columns = right instrument',
        'common_observed_matrix': [[0, 2, 0, 0, 0], [1, 0, 0, 0, 0],
                                   [0, 0, 0, 0, 0], [0, 0, 0, 0, 0], [0, 0, 0, 0, 0]],
        'assigned_pairs': 5, 'complete_pairs': 3,
        'transitions': [{'left': 'yes', 'right': 'no', 'count': 2},
                        {'left': 'no', 'right': 'yes', 'count': 1},
                        {'left': None, 'right': 'yes', 'count': 1},
                        {'left': None, 'right': None, 'count': 1}],
        'dispositions': [{'left': 'observed', 'right': 'observed', 'count': 3},
                         {'left': 'technical_missing', 'right': 'observed', 'count': 1},
                         {'left': 'source_unavailable', 'right': 'source_unavailable', 'count': 1}],
    }
    zero = copy.deepcopy(summary)
    zero.update(common_observed_matrix=[[0] * 5 for _ in range(5)], complete_pairs=0,
                transitions=[{'left': None, 'right': None, 'count': 5}],
                dispositions=[{'left': 'source_unavailable', 'right': 'source_unavailable', 'count': 5}])
    rows = [{'panel_id': 'some', 'summary': summary}, {'panel_id': 'zero', 'summary': zero}]
    config = {'axis_direction': {'rows': 'left', 'columns': 'right'}, 'columns': 2,
              'title': 'Synthetic paired counts', 'subtitle': 'Independent fixture panels',
              'footer': 'Missing pairs retain assigned population; synthetic data only.',
              'file_stem': 'paired_counts', 'panels': [
                  {'panel_id': key, 'title': title, 'left': 'presence', 'right': 'strongly_favors',
                   'row_label': 'Presence', 'column_label': 'Strong evidence'}
                  for key, title in [('zero', 'Zero complete pairs'), ('some', 'Asymmetric transitions')]]}
    return rows, config


class MatrixPanelTests(unittest.TestCase):
    def test_explicit_order_preserves_direction_zero_cells_and_missing_pairs(self):
        rows, config = matrix_fixture()
        before = copy.deepcopy((rows, config))
        prepared = figures.prepare_matrix_panels(rows, config)
        self.assertEqual([p['panel_id'] for p in prepared], ['zero', 'some'])
        self.assertEqual([(p['complete_pairs'], p['assigned_pairs'], p['missing_pairs'])
                          for p in prepared], [(0, 5, 5), (3, 5, 2)])
        self.assertEqual(prepared[1]['matrix'][0][1], 2)
        self.assertEqual(prepared[1]['matrix'][1][0], 1)
        self.assertEqual((rows, config), before)

    def test_transposition_and_inconsistent_totals_refuse(self):
        for change in ('transpose', 'transition_total', 'matrix_total', 'complete', 'negative', 'boolean'):
            with self.subTest(change=change):
                rows, config = matrix_fixture()
                row = rows[0]['summary']
                if change == 'transpose':
                    row['common_observed_matrix'] = list(map(list, zip(*row['common_observed_matrix'])))
                elif change == 'transition_total':
                    row['transitions'][-1]['count'] += 1
                elif change == 'matrix_total':
                    row['common_observed_matrix'][0][0] = 1
                elif change == 'complete':
                    row['complete_pairs'] = 6
                elif change == 'negative':
                    row['transitions'][0]['count'] = -1
                else:
                    row['common_observed_matrix'][0][0] = False
                with self.assertRaises(ValueError):
                    figures.prepare_matrix_panels(rows, config)

    def test_missingness_direction_and_duplicate_transitions_refuse(self):
        rows, config = matrix_fixture()
        rows[0]['summary']['dispositions'][1].update(left='observed', right='technical_missing')
        with self.assertRaisesRegex(ValueError, 'matrix_missingness_counts_disagree'):
            figures.prepare_matrix_panels(rows, config)
        rows, config = matrix_fixture()
        rows[0]['summary']['transitions'].append({'left': 'yes', 'right': 'no', 'count': 0})
        with self.assertRaisesRegex(ValueError, 'invalid_matrix_transition'):
            figures.prepare_matrix_panels(rows, config)

    def test_explicit_panel_membership_and_axes_are_required(self):
        for change in ('reverse_axes', 'reverse_pair', 'title', 'missing_panel', 'duplicate_panel'):
            with self.subTest(change=change):
                rows, config = matrix_fixture()
                if change == 'reverse_axes':
                    config['axis_direction'] = {'rows': 'right', 'columns': 'left'}
                elif change == 'reverse_pair':
                    config['panels'][0].update(left='strongly_favors', right='presence')
                elif change == 'title':
                    config['panels'][0]['title'] = ''
                elif change == 'missing_panel':
                    rows.pop()
                else:
                    rows.append(copy.deepcopy(rows[0]))
                with self.assertRaises(ValueError):
                    figures.prepare_matrix_panels(rows, config)

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'optional matplotlib dependency')
    def test_render_keeps_counts_axes_full_key_and_zero_complete_population(self):
        from matplotlib.axes import Axes
        from matplotlib.figure import Figure
        rows, config = matrix_fixture()
        images, cell_text, figure_text, xlabels, ylabels, titles = [], [], [], [], [], []
        originals = {name: getattr(Axes, name) for name in ('imshow', 'text', 'set_xlabel', 'set_ylabel', 'set_title')}
        figure_text_original = Figure.text

        def imshow(ax, values, *args, **kwargs):
            images.append((copy.deepcopy(values), kwargs))
            return originals['imshow'](ax, values, *args, **kwargs)

        def text(ax, x, y, value, *args, **kwargs):
            cell_text.append(str(value))
            return originals['text'](ax, x, y, value, *args, **kwargs)

        def record_axis(name, target):
            def call(ax, label, *args, **kwargs):
                target.append(label)
                return originals[name](ax, label, *args, **kwargs)
            return call

        def caption(figure, x, y, value, *args, **kwargs):
            figure_text.append(str(value))
            return figure_text_original(figure, x, y, value, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(Axes, 'imshow', imshow), mock.patch.object(Axes, 'text', text), \
                    mock.patch.object(Axes, 'set_xlabel', record_axis('set_xlabel', xlabels)), \
                    mock.patch.object(Axes, 'set_ylabel', record_axis('set_ylabel', ylabels)), \
                    mock.patch.object(Axes, 'set_title', record_axis('set_title', titles)), \
                    mock.patch.object(Figure, 'text', caption):
                paths = figures.plot_pair_matrices(rows, temporary, config)
            self.assertTrue(paths['png'].read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn('<svg', paths['svg'].read_text())
        self.assertEqual(images[0][0], [[0] * 5 for _ in range(5)])
        self.assertEqual(images[1][0], rows[0]['summary']['common_observed_matrix'])
        self.assertEqual([v['origin'] for _, v in images], ['upper', 'upper'])
        self.assertEqual([v['vmax'] for _, v in images], [2, 2])
        self.assertEqual(cell_text[:25], ['0'] * 25)
        self.assertIn('Complete 0 / assigned 5; missing 5', titles[0])
        self.assertIn('Right: Strong evidence', xlabels)
        self.assertIn('Left: Presence', ylabels)
        full_text = '\n'.join(figure_text)
        for label in figures.descriptive.CATEGORIES:
            self.assertIn(label, full_text)
        self.assertIn('Rows = left; columns = right', full_text)
        self.assertNotIn('%', full_text)

    def test_existing_either_output_is_preserved_without_creating_other_output(self):
        for suffix in ('png', 'svg'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                rows, config = matrix_fixture()
                root = Path(temporary)
                existing = root / ('paired_counts.' + suffix)
                existing.write_bytes(b'original bytes')
                with self.assertRaises(FileExistsError):
                    figures.plot_pair_matrices(rows, root, config)
                self.assertEqual(existing.read_bytes(), b'original bytes')
                self.assertEqual(list(root.iterdir()), [existing])


if __name__ == '__main__':
    unittest.main()
