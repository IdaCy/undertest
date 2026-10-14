import io
import math
from pathlib import Path


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _interval(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_number(x) for x in value) and -1 <= value[0] <= value[1] <= 1)


def prepare_effect_panels(rows, config):
    """Validate already computed differences without calculating any estimator."""
    contrasts, panels = config['contrasts'], config['panels']
    for items, key in ((contrasts, 'contrast'), (panels, 'panel')):
        _require(isinstance(items, (list, tuple)) and items, 'explicit_roster_required')
        _require(all(isinstance(item, dict) and _text(item.get(key))
                     and _text(item.get('label')) for item in items), 'invalid_roster_item')
        _require(len({item[key] for item in items}) == len(items), 'duplicate_roster_item')
    for panel in panels:
        _require(_text(panel.get('uncertainty_label')) and _text(panel.get('assumption'))
                 and _text(panel.get('unit_label')),
                 'explicit_uncertainty_and_assumption_required')
        _require(type(panel.get('family_size')) is int and panel['family_size'] > 0,
                 'explicit_correction_family_required')
    alpha = config['adjusted_alpha']
    _require(_number(alpha) and 0 < alpha < 1, 'invalid_adjusted_alpha')
    lookup = {}
    for row in rows:
        _require(isinstance(row, dict), 'invalid_effect_row')
        key = row['contrast'], row['panel']
        _require(key not in lookup, 'duplicate_effect_cell')
        _require(_interval(row['missing_label_bounds']) and _interval(row['uncertainty_bounds']),
                 'invalid_effect_interval')
        low, high = row['missing_label_bounds']
        left, right = row['uncertainty_bounds']
        _require(left <= low <= high <= right, 'uncertainty_does_not_cover_missingness')
        _require(all(type(row[k]) is int and row[k] >= 0
                     for k in ('assigned', 'observed', 'missing', 'task_count', 'cluster_count')),
                 'invalid_effect_counts')
        _require(row['assigned'] > 0 and row['observed'] + row['missing'] == row['assigned']
                 and 0 < row['cluster_count'] <= row['assigned']
                 and 0 < row['task_count'] <= row['assigned'],
                 'effect_denominators_disagree')
        _require(_number(row['adjusted_p']) and 0 <= row['adjusted_p'] <= 1,
                 'invalid_adjusted_p')
        lookup[key] = row
    expected = {(c['contrast'], p['panel']) for c in contrasts for p in panels}
    _require(set(lookup) == expected, 'effect_grid_membership_changed')
    prepared = []
    for contrast in contrasts:
        reference = lookup[contrast['contrast'], panels[0]['panel']]
        for panel in panels:
            row = lookup[contrast['contrast'], panel['panel']]
            _require(row['family_size'] == panel['family_size'], 'correction_family_changed')
            for key in ('missing_label_bounds', 'assigned', 'observed', 'missing', 'task_count'):
                _require(row[key] == reference[key], 'cross_panel_effect_or_denominator_changed')
            prepared.append({**row, 'missing_label_bounds': list(row['missing_label_bounds']),
                             'uncertainty_bounds': list(row['uncertainty_bounds']),
                             'passes_adjusted_threshold': row['adjusted_p'] <= alpha})
    return prepared


def plot_effect_panels(rows, destination, config):
    """Save fixed-roster PNG/SVG panels, retaining missing-label and uncertainty ranges."""
    prepared = prepare_effect_panels(rows, config)
    stem = config['file_stem']
    _require(_text(stem) and stem not in ('.', '..') and Path(stem).name == stem,
             'invalid_effect_figure_stem')
    _require(all(_text(config.get(key)) for key in ('title', 'subtitle', 'footer')),
             'explicit_figure_captions_required')
    maximum = config['maximum_output_bytes']
    _require(type(maximum) is int and maximum > 0, 'explicit_output_bound_required')
    destination = Path(destination)
    paths = {kind: destination / (stem + '.' + kind) for kind in ('png', 'svg')}
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise FileExistsError('effect_figure_exists')

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D

    contrasts, panels = config['contrasts'], config['panels']
    width = 5.7 * len(panels) + 3
    height = 0.35 * len(contrasts) + 2.8
    figure = Figure(figsize=(width, height))
    FigureCanvasAgg(figure)
    axes = figure.subplots(1, len(panels), sharey=True, squeeze=False)[0]
    lookup = {(r['contrast'], r['panel']): r for r in prepared}
    try:
        for ax, panel in zip(axes, panels):
            for index, contrast in enumerate(contrasts):
                row = lookup[contrast['contrast'], panel['panel']]
                uncertainty = [100 * x for x in row['uncertainty_bounds']]
                effect = [100 * x for x in row['missing_label_bounds']]
                color = '#086c79' if row['passes_adjusted_threshold'] else '#737373'
                ax.hlines(index, *uncertainty, color=color, linewidth=1.1, alpha=0.7)
                ax.plot(uncertainty, [index, index], '|', color=color, markersize=5)
                ax.hlines(index, *effect, color=color, linewidth=4.2)
                if effect[0] == effect[1]:
                    ax.plot(effect[:1], [index], '|', color=color, markersize=7)
                ax.text(104, index, f"{row['observed']}/{row['assigned']}  {row['cluster_count']}",
                        fontsize=7.3, va='center')
            ax.axvline(0, color='#a6a6a6', linestyle=':', linewidth=0.8)
            ax.set_xlim(-102, 145)
            ax.set_xticks([-100, -50, 0, 50, 100])
            ax.set_xlabel('Treatment − unchanged baseline (percentage points)', fontsize=9)
            ax.set_title(panel['label'] + '\n' + panel['uncertainty_label'] + '\n'
                         + panel['assumption'], fontsize=9, pad=22)
            ax.text(104, -0.9, 'observed/assigned  ' + panel['unit_label'], fontsize=7.3)
            ax.spines[['top', 'right']].set_visible(False)
            ax.grid(axis='y', color='#eeeeee', linewidth=0.45)
        axes[0].set_yticks(range(len(contrasts)), [r['label'] for r in contrasts], fontsize=8.5)
        axes[0].set_ylim(len(contrasts) - 0.3, -0.7)
        figure.suptitle(config['title'], fontsize=15, y=0.989)
        figure.text(0.5, 0.963, config['subtitle'], ha='center', fontsize=9)
        figure.legend([
            Line2D([0], [0], color='#555555', linewidth=4.2),
            Line2D([0], [0], color='#555555', linewidth=1.1),
            Line2D([0], [0], color='#086c79', linewidth=2),
            Line2D([0], [0], color='#737373', linewidth=2)],
            ['Missing-label effect range (not a confidence interval)', 'Pointwise uncertainty interval',
             'Adjusted p ≤ ' + str(config['adjusted_alpha']), 'Adjusted p > ' + str(config['adjusted_alpha'])],
            loc='lower center', bbox_to_anchor=(0.5, 0.038), ncol=2, fontsize=9)
        figure.text(0.5, 0.01, config['footer'], ha='center', fontsize=8.5)
        figure.tight_layout(rect=(0, 0.08, 1, 0.94), w_pad=1.3)
        buffers = {}
        for kind in paths:
            stream = io.BytesIO()
            figure.savefig(stream, format=kind, dpi=130)
            buffers[kind] = stream.getvalue()
            _require(sum(map(len, buffers.values())) <= maximum, 'effect_figure_output_bound')
        destination.mkdir(parents=True, exist_ok=True)
        for kind, path in paths.items():
            with path.open('xb') as stream:
                stream.write(buffers[kind])
    finally:
        figure.clear()
    return paths
