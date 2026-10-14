import copy
import tempfile
import unittest
from pathlib import Path

from causes import campaign_contrast_figures as figures


def fixture():
    panels = [{'panel': 'draw', 'label': 'Draw', 'uncertainty_label': '95% interval',
               'assumption': 'Independent draws', 'family_size': 4, 'unit_label': 'draws'},
              {'panel': 'task', 'label': 'Task', 'uncertainty_label': '95% bound',
               'assumption': 'Independent tasks', 'family_size': 4, 'unit_label': 'tasks'}]
    config = {'contrasts': [{'contrast': 'a', 'label': 'A'}, {'contrast': 'b', 'label': 'B'}],
              'panels': panels, 'adjusted_alpha': .05, 'file_stem': 'effects',
              'title': 'Saved effects', 'subtitle': 'Different uncertainty assumptions',
              'footer': 'Fixed tasks; missing labels retained.', 'maximum_output_bytes': 1048576}
    rows = []
    for contrast, bounds, p in [('a', [.1, .2], .01), ('b', [0, 0], 1.)]:
        for panel in panels:
            rows.append({'contrast': contrast, 'panel': panel['panel'],
                         'missing_label_bounds': bounds[:], 'uncertainty_bounds': [-.4, .5],
                         'assigned': 20, 'observed': 19, 'missing': 1,
                         'task_count': 4, 'cluster_count': 4,
                         'adjusted_p': p, 'family_size': 4})
    return rows, config


class EffectPanelsTests(unittest.TestCase):
    def test_exact_grid_and_source_unchanged(self):
        rows, config = fixture()
        original = copy.deepcopy(rows)
        prepared = figures.prepare_effect_panels(rows[::-1], config)
        self.assertEqual([(r['contrast'], r['panel']) for r in prepared],
                         [('a', 'draw'), ('a', 'task'), ('b', 'draw'), ('b', 'task')])
        self.assertEqual([r['passes_adjusted_threshold'] for r in prepared], [True, True, False, False])
        prepared[0]['missing_label_bounds'][0] = -.5
        self.assertEqual(rows, original)

    def test_missing_extra_or_duplicate_cells_refused(self):
        rows, config = fixture()
        for changed in (rows[:-1], rows + [rows[0]], rows + [{**rows[0], 'contrast': 'extra'}]):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                figures.prepare_effect_panels(changed, config)

    def test_invalid_numbers_bounds_counts_and_p_values(self):
        changes = [('missing_label_bounds', [0, float('nan')]),
                   ('missing_label_bounds', [.3, -.1]),
                   ('uncertainty_bounds', [0, .15]),
                   ('uncertainty_bounds', [-2, 1]), ('adjusted_p', True),
                   ('adjusted_p', float('inf')), ('adjusted_p', -.1),
                   ('observed', 20), ('missing', -1), ('task_count', 0),
                   ('cluster_count', 21), ('family_size', 3)]
        for key, value in changes:
            rows, config = fixture()
            rows[0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                figures.prepare_effect_panels(rows, config)

    def test_cross_panel_denominators_or_effects_refused(self):
        for change in ({'assigned': 21, 'missing': 2}, {'task_count': 5},
                       {'missing_label_bounds': [.1, .3]}):
            rows, config = fixture()
            rows[1].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'cross_panel'):
                figures.prepare_effect_panels(rows, config)

    def test_explicit_labels_and_assumptions_required(self):
        for key in ('label', 'assumption', 'uncertainty_label', 'unit_label'):
            rows, config = fixture()
            config['panels'][0][key] = ''
            with self.subTest(key=key), self.assertRaises(ValueError):
                figures.prepare_effect_panels(rows, config)

    def test_existing_output_preserved_and_path_escape_refused(self):
        rows, config = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            saved = destination / 'effects.svg'
            saved.write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                figures.plot_effect_panels(rows, destination, config)
            self.assertEqual(saved.read_bytes(), b'original')
            self.assertFalse((destination / 'effects.png').exists())
            config['file_stem'] = '../escape'
            with self.assertRaises(ValueError):
                figures.plot_effect_panels(rows, destination, config)


if __name__ == '__main__':
    unittest.main()
