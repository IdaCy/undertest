"""Join qualified categorical condition tables without recomputing statistics."""

from copy import deepcopy
from collections.abc import Mapping


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _names(values, reason):
    _require(isinstance(values, (list, tuple)) and bool(values), reason)
    _require(all(type(value) is str and bool(value) for value in values), reason)
    _require(len(set(values)) == len(values), reason)
    return list(values)


def _count(value, maximum=None):
    return type(value) is int and value >= 0 and (maximum is None or value <= maximum)


def _distribution(value, count):
    _require(isinstance(value, Mapping) and _count(value.get('assigned')) and value['assigned'] == count,
             'distribution_assignment_mismatch')
    observed = value.get('observed')
    _require(_count(observed, count), 'invalid_observed_count')
    categories, dispositions = value.get('categories'), value.get('dispositions')
    _require(isinstance(categories, Mapping) and isinstance(dispositions, Mapping),
             'invalid_distribution_counts')
    _require(all(type(key) is str and key for key in categories)
             and all(type(key) is str and key for key in dispositions)
             and all(_count(n) for n in categories.values())
             and all(_count(n) for n in dispositions.values()), 'invalid_distribution_counts')
    _require(sum(categories.values()) == observed
             and dispositions.get('observed', 0) == observed
             and sum(dispositions.values()) == count, 'distribution_count_total')


def _paired(value, count):
    _require(isinstance(value, Mapping) and _count(value.get('assigned_pairs')) and value['assigned_pairs'] == count
             and _count(value.get('complete_pairs'), count)
             and _count(value.get('same_physical_measurement_pairs'), count),
             'paired_assignment_mismatch')


def compose_condition_readout(declared, natural_supplement, families, labels, *, baseline):
    """Copy qualified distributions and keep declared-control and natural-baseline comparisons distinct.

    The caller supplies frozen ordered task/level rosters and independently sourced display labels. Supplemental tables are required exactly for families whose declared control differs from baseline. Their original-level distributions must equal the declared tables. Reference rows and uncomputed comparisons remain explicit statuses, never invented zeros or reversed contrasts. Upstream provenance, category meanings and statistical validity remain the caller's responsibility.
    """
    _require(type(baseline) is str and bool(baseline), 'invalid_baseline')
    _require(all(isinstance(value, Mapping) for value in
                 (declared, natural_supplement, families, labels)), 'invalid_condition_maps')
    _require(bool(families) and set(declared) == set(families) == set(labels), 'family_membership_mismatch')
    needed = {family for family, plan in families.items() if plan['control'] != baseline}
    _require(set(natural_supplement) == needed, 'supplement_family_membership_mismatch')
    result = []
    for family, plan in families.items():
        _require(type(family) is str and bool(family), 'invalid_family')
        tasks = _names(plan['tasks'], 'invalid_task_roster')
        levels = _names(plan['levels'], 'invalid_level_roster')
        control, count = plan['control'], len(tasks)
        _require(control in levels, 'control_not_in_original_levels')
        table = declared[family]
        _require(table['control'] == control and _count(table['task_count']) and table['task_count'] == count,
                 'declared_control_or_roster_mismatch')
        _require(set(table['distributions']) == set(levels)
                 and set(table['level_minus_control']) == set(levels) - {control},
                 'declared_level_membership_mismatch')
        natural = natural_supplement[family] if family in needed else table
        _require(natural['control'] == baseline and _count(natural['task_count']) and natural['task_count'] == count,
                 'natural_control_or_roster_mismatch')
        display_levels = levels if baseline in levels else [baseline, *levels]
        _require(set(natural['distributions']) == set(display_levels)
                 and set(natural['level_minus_control']) == set(display_levels) - {baseline},
                 'natural_level_membership_mismatch')
        _require(all(natural['distributions'][level] == table['distributions'][level]
                     for level in levels), 'original_distribution_changed')
        display = labels[family]
        _require(type(display['family']) is str and bool(display['family'])
                 and set(display['levels']) == set(display_levels)
                 and all(type(label) is str and label for label in display['levels'].values()),
                 'display_label_membership_mismatch')

        def comparison(source, reference, level):
            if level == reference:
                return {'status': 'reference', 'reference': reference, 'value': None}
            if level not in source['level_minus_control']:
                return {'status': 'not_tabulated', 'reference': reference, 'value': None}
            value = source['level_minus_control'][level]
            _paired(value, count)
            return {'status': 'comparison', 'reference': reference, 'value': deepcopy(value)}

        rows = []
        for level in display_levels:
            distribution = natural['distributions'][level]
            _distribution(distribution, count)
            rows.append({'condition': level, 'label': display['levels'][level],
                         'distribution': deepcopy(distribution),
                         'declared_comparison': comparison(table, control, level),
                         'natural_comparison': comparison(natural, baseline, level)})
        result.append({'family': family, 'label': display['family'], 'tasks_in_order': tasks,
                       'assigned': count, 'declared_control': control, 'natural_baseline': baseline,
                       'references_identical': control == baseline, 'levels': rows})
    return {'baseline': baseline, 'families': result,
            'numeric_policy': 'Accepted input objects copied exactly; no new estimate, contrast inversion or implied zero.'}


def distribution_plot_rows(family_view, *, method, stage, threshold):
    """Adapt one condition family to the existing complete-grid distribution renderer."""
    _require(all(type(value) is str and value for value in (method, stage, threshold)),
             'invalid_measurement_labels')
    rows = family_view['levels']
    _names([row['condition'] for row in rows], 'invalid_view_levels')
    return [{**deepcopy(row['distribution']), 'method': method, 'stage': stage,
             'awareness_type': row['condition'], 'threshold': threshold} for row in rows]
