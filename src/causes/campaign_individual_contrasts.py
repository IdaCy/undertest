from __future__ import annotations

from collections import Counter

from .campaign_analysis import (
    READER_LABELS,
    SELF_LABELS,
    bounded_inference,
    contrast,
    holm,
)


def _measurement(record):
    if record is None:
        return 'pending', None
    if not isinstance(record, dict):
        raise ValueError('invalid_measurement_record')
    status = record.get('status')
    label = record.get('label')
    if status == 'observed':
        if label not in SELF_LABELS + READER_LABELS:
            raise ValueError('invalid_observed_label')
    elif status in ('missing', 'pending'):
        if label is not None:
            raise ValueError('unobserved_record_has_label')
        if status == 'missing' and (
            not isinstance(record.get('reason'), str) or not record['reason'].strip()
        ):
            raise ValueError('explicit_missing_reason_required')
    else:
        raise ValueError('invalid_measurement_status')
    return status, label


def _epoch_assignments(assignment, epochs):
    result = {}
    names = sorted({epochs[run] for arms in assignment['members'].values()
                    for ids in arms.values() for run in ids})
    for epoch in names:
        members = {}
        for task, arms in assignment['members'].items():
            members[task] = {
                arm: [run for run in arms[arm] if epochs[run] == epoch]
                for arm in ('treatment', 'control')
            }
        row = {'factor': assignment['factor'], 'members': members}
        contrast(row, {}, release=False, family_size=1)
        result[epoch] = row
    return result


def individual_contrasts(assignments, readouts, *, expected_factors,
                         primary_readouts, epochs=None):
    """Release each fixed contrast only when both primary readouts are terminal.

    Missing measurements require an explicit reason; absent entries are pending.
    Secondary readouts and epoch splits remain descriptive. Pending hypotheses
    contribute p=1 to the fixed experiment-wide Holm family.
    """
    assignments = list(assignments)
    expected = list(expected_factors)
    primary = list(primary_readouts)
    if not expected or len(set(expected)) != len(expected):
        raise ValueError('invalid_frozen_contrast_family')
    actual = [row['factor'] for row in assignments]
    if len(set(actual)) != len(actual) or set(actual) != set(expected):
        raise ValueError('frozen_contrast_membership_mismatch')
    if len(primary) != 2 or len(set(primary)) != 2 or not set(primary) <= set(readouts):
        raise ValueError('two_frozen_primary_readouts_required')
    if any(not isinstance(name, str) or not name for name in expected + list(readouts)):
        raise ValueError('invalid_contrast_or_readout_name')

    assigned_ids = set()
    assignments_by_name = {}
    for assignment in assignments:
        contrast(assignment, {}, release=False, family_size=1)
        assignments_by_name[assignment['factor']] = assignment
        assigned_ids.update(run for arms in assignment['members'].values()
                            for arm in ('treatment', 'control') for run in arms[arm])
    records = {}
    for name, values in readouts.items():
        if not isinstance(values, dict) or not set(values) <= assigned_ids:
            raise ValueError('readout_outside_frozen_assignment')
        records[name] = {run: _measurement(value) for run, value in values.items()}
    if epochs is not None and (
        not isinstance(epochs, dict) or set(epochs) != assigned_ids
        or any(not isinstance(value, str) or not value for value in epochs.values())
    ):
        raise ValueError('exact_frozen_epoch_membership_required')

    output = []
    pvalues = {}
    for factor in expected:
        assignment = assignments_by_name[factor]
        ids = {run for arms in assignment['members'].values()
               for arm in ('treatment', 'control') for run in arms[arm]}
        census = {
            name: Counter(values.get(run, ('pending', None))[0] for run in ids)
            for name, values in records.items()
        }
        released = all(census[name]['pending'] == 0 for name in primary)
        epoch_rows = _epoch_assignments(assignment, epochs) if epochs is not None else {}
        row = {'factor': factor, 'primary_inference_released': released,
               'assigned_unique_runs': len(ids), 'readouts': {}}
        for name, values in records.items():
            ready = census[name]['pending'] == 0
            entry = {
                'primary': name in primary,
                'counts': {state: census[name][state] for state in ('observed', 'missing', 'pending')},
                'all_assigned_terminal': ready,
                'analysis': None,
                'epochs_descriptive': {},
            }
            if released and ready:
                labels = {run: values.get(run, ('pending', None))[1] for run in ids}
                infer = name in primary
                result = contrast(assignment, labels, release=infer, family_size=2)
                result['pointwise_approximate_interval_95'] = bounded_inference(
                    *result['missing_assigned_sample_effect_bounds'],
                    result['se_upper_bound'], 1, release=infer,
                )['bonferroni_approximate_interval']
                result['primary_alpha'] = .025 if infer else None
                result['primary_rejects'] = result['approximate_p'] <= .025 if infer else None
                entry['analysis'] = result
                entry['epochs_descriptive'] = {
                    epoch: contrast(part, labels, release=False, family_size=1)
                    for epoch, part in epoch_rows.items()
                }
            row['readouts'][name] = entry
            if name in primary:
                pvalues[(factor, name)] = entry['analysis']['approximate_p'] if released else 1.0
        output.append(row)

    adjusted = holm(pvalues)
    for row in output:
        for name in primary:
            entry = row['readouts'][name]
            entry['holm_placeholder'] = not row['primary_inference_released']
            entry['experiment_wide_holm_p'] = (
                adjusted[(row['factor'], name)] if row['primary_inference_released'] else None
            )
    return {
        'frozen_factors': expected,
        'primary_readouts': primary,
        'experiment_wide_family_size': len(pvalues),
        'all_primary_contrasts_released': all(row['primary_inference_released'] for row in output),
        'secondary_inference_released': False,
        'results': output,
    }
