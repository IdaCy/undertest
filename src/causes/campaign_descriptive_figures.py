"""Paired categorical matrices and figures over explicit descriptive tables."""

from pathlib import Path

from . import campaign_descriptive as descriptive


def pair_summary(left, right, desc=descriptive):
    if any(a['identity'][:2] != b['identity'][:2] for a, b in zip(left, right)):
        raise ValueError('unmatched_pair_context')
    return matrix_summary(desc.paired(left, right), desc)


def stage_pair_summary(early, end, desc=descriptive):
    for first, second in zip(early, end):
        a, b = first['identity'], second['identity']
        if len(a) != 5 or len(b) != 5 or a[1] != 'early' or b[1] != 'end' or a[0] != b[0] or a[2:] != b[2:]:
            raise ValueError('unmatched_stage_instrument')
    return matrix_summary(desc.paired(early, end), desc)


def matrix_summary(result, desc=descriptive):
    labels = list(desc.CATEGORIES)
    matrix = [[0 for _ in labels] for _ in labels]
    for row in result['transitions']:
        if row['left'] is not None and row['right'] is not None:
            matrix[labels.index(row['left'])][labels.index(row['right'])] += row['count']
    if sum(map(sum, matrix)) != result['complete_pairs'] or sum(r['count'] for r in result['transitions']) != result['assigned_pairs']:
        raise ValueError('pair_matrix_denominators')
    result.update(matrix_labels=labels, common_observed_matrix=matrix,
                  matrix_direction='rows = left instrument; columns = right instrument',
                  matrix_denominator='complete_pairs; full transitions and dispositions retain assigned_pairs including nulls')
    return result


def _roster(value, name):
    if (not isinstance(value, (list, tuple)) or not value
            or any(type(item) is not str or not item for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('invalid_plot_roster:' + name)
    return tuple(value)


def _count(value):
    return type(value) is int and value >= 0


def prepare_plot_rows(rows, config):
    """Validate the complete caller-selected grid and retain assigned denominators."""
    methods, stages, awareness, thresholds = (
        _roster(config[key], key) for key in ('methods', 'stages', 'awareness_types', 'thresholds'))
    missing = config['missingness']
    if not isinstance(missing, (list, tuple)):
        raise ValueError('explicit_missingness_styles_required')
    statuses = [item['status'] for item in missing]
    if (len(set(statuses)) != len(statuses) or 'observed' in statuses
            or any(type(status) is not str or not status for status in statuses)):
        raise ValueError('invalid_missingness_styles')
    for item in missing:
        if any(type(item[key]) is not str or not item[key] for key in ('label', 'color')):
            raise ValueError('invalid_missingness_style')
    lookup = {}
    for row in rows:
        key = tuple(row[name] for name in ('method', 'stage', 'awareness_type', 'threshold'))
        if key in lookup:
            raise ValueError('duplicate_plot_cell')
        lookup[key] = row
    expected = [(m, s, a, t) for m in methods for s in stages for a in awareness for t in thresholds]
    if set(lookup) != set(expected):
        raise ValueError('plot_grid_membership_changed')
    prepared = []
    for key in expected:
        row = lookup[key]
        assigned, observed = row['assigned'], row['observed']
        categories, dispositions = row['categories'], row['dispositions']
        if (not _count(assigned) or assigned == 0 or not _count(observed)
                or set(categories) != set(descriptive.CATEGORIES)
                or any(not _count(value) for value in categories.values())
                or any(not _count(value) for value in dispositions.values())):
            raise ValueError('invalid_plot_counts')
        if set(dispositions) - {'observed', *statuses}:
            raise ValueError('unmapped_plot_disposition')
        values = [categories[category] for category in descriptive.CATEGORIES]
        values += [dispositions.get(status, 0) for status in statuses]
        if (sum(categories.values()) != observed or dispositions.get('observed', 0) != observed
                or sum(dispositions.values()) != assigned or sum(values) != assigned):
            raise ValueError('plot_count_total')
        prepared.append({'key': key, 'assigned': assigned, 'observed': observed,
                         'values': values, 'percentages': [value / assigned * 100 for value in values]})
    return prepared


def plot_distributions(rows, destination, config):
    """Write PNG/SVG figures from explicit rows, rosters, styles and captions.

    Each row supplies its assigned population. Missing statuses must have an
    explicit style, so pending or unavailable measurements cannot disappear.
    Existing figure files are preserved. Matplotlib is an optional dependency.
    """
    prepared = prepare_plot_rows(rows, config)
    methods, stages, awareness, thresholds = (
        tuple(config[key]) for key in ('methods', 'stages', 'awareness_types', 'thresholds'))
    stem = config.get('file_stem', 'awareness_wordings')
    minimum = config.get('annotate_min_count', 4)
    if type(stem) is not str or not stem or stem in ('.', '..') or Path(stem).name != stem:
        raise ValueError('invalid_figure_stem')
    if not _count(minimum):
        raise ValueError('invalid_annotation_count')
    destination = Path(destination)
    paths = {kind: destination / (stem + '.' + kind) for kind in ('png', 'svg')}
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise FileExistsError('figure_output_exists')

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Patch

    missing = config['missingness']
    colors = ['#16816a', '#aa4b58', '#756bb1', '#d4a83f', '#6a99c2'] + [r['color'] for r in missing]
    hatches = [None] * len(descriptive.CATEGORIES) + [r.get('hatch') for r in missing]
    category_labels = config.get('category_labels', {})
    labels = [category_labels.get(key, key) for key in descriptive.CATEGORIES] + [r['label'] for r in missing]
    lookup = {tuple(row['key']): row for row in prepared}
    figure = Figure(figsize=config.get('figsize', (9.5 * len(stages), 8 * len(methods))))
    FigureCanvasAgg(figure)
    axes = figure.subplots(len(methods), len(stages), sharex=True, squeeze=False)
    instruments = [(a, t) for a in awareness for t in thresholds]
    y_positions = [i + i // len(thresholds) * .45 for i in range(len(instruments))]
    try:
        for row_index, method in enumerate(methods):
            for col_index, stage in enumerate(stages):
                ax = axes[row_index, col_index]
                names = []
                for (kind, threshold), y in zip(instruments, y_positions):
                    data = lookup[method, stage, kind, threshold]
                    left = 0
                    for index, (count, width) in enumerate(zip(data['values'], data['percentages'])):
                        ax.barh(y, width, left=left, color=colors[index], edgecolor='white',
                                linewidth=.35, hatch=hatches[index])
                        if count >= minimum:
                            ax.text(left + width / 2, y, str(count), ha='center', va='center', fontsize=8,
                                    color='white' if index in (0, 1, 2) else '#222222')
                        left += width
                    ax.text(102, y, f"{data['observed']}/{data['assigned']}", va='center', fontsize=8)
                    names.append(config.get('awareness_labels', {}).get(kind, kind) + ' · '
                                 + config.get('threshold_labels', {}).get(threshold, threshold))
                ax.set_yticks(y_positions, names, fontsize=9)
                ax.invert_yaxis()
                ax.set_xlim(0, 115)
                ax.set_xticks([0, 25, 50, 75, 100], ['0%', '25%', '50%', '75%', '100%'])
                ax.set_title(config.get('method_labels', {}).get(method, method) + ' · '
                             + config.get('stage_labels', {}).get(stage, stage), fontsize=13)
                ax.set_xlabel(config.get('xlabel', 'Fraction of assigned trajectories; right label = observed / assigned'))
                ax.spines[['top', 'right']].set_visible(False)
        figure.suptitle(config.get('title', ''), fontsize=17, y=.992)
        figure.text(.5, .965, config.get('subtitle', ''), ha='center', fontsize=10)
        figure.legend([Patch(facecolor=color, edgecolor='#555', hatch=hatch)
                       for color, hatch in zip(colors, hatches)], labels, loc='lower center',
                      ncol=4, fontsize=10, bbox_to_anchor=(.5, .023))
        figure.text(.5, .005, config.get('footer', ''), ha='center', fontsize=9)
        figure.tight_layout(rect=(0, .075, 1, .945))
        destination.mkdir(parents=True, exist_ok=True)
        for kind, path in paths.items():
            with path.open('xb') as stream:
                figure.savefig(stream, format=kind, dpi=170)
    finally:
        figure.clear()
    return paths
