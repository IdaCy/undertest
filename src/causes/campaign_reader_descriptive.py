"""Descriptive reuse of verified saved reader results on fixed assignments.

The caller verifies immutable custody and result pins before supplying rows and
``load_result``. No requests, response parsing, inference or writes occur here.
"""

from collections import Counter
from collections.abc import Mapping
import hashlib
import json

from .campaign_analysis import READER_LABELS, contrast, reader_label
from .campaign_descriptive import TYPES, THRESHOLDS, READER_NAMES, distribution


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _selection(values, allowed, reason):
    _require(isinstance(values, (list, tuple)) and values
             and all(isinstance(x, str) and x in allowed for x in values)
             and len(set(values)) == len(values), reason)
    return tuple(values)


def _descriptive(assignment, labels, awareness_type, threshold, family_size, epoch=None):
    value = contrast(assignment, labels, release=False, family_size=family_size)
    # Inferential fields are deliberately excluded, including variance bounds.
    fields = ('factor', 'task_count', 'assigned', 'observed',
              'all_assigned_observed', 'equal_task_effect',
              'category_equal_task_weights', 'missing_assigned_sample_effect_bounds')
    result = {key: value[key] for key in fields}
    result.update(awareness_type=awareness_type, threshold=threshold, epoch=epoch,
                  missing={arm: value['assigned'][arm] - value['observed'][arm]
                           for arm in ('treatment', 'control')})
    return result


def project_saved_reader_awareness(assignments, custody_rows, load_result, *,
                                   awareness_types=TYPES, thresholds=THRESHOLDS):
    """Return physical distributions and equal-task contrast/epoch summaries.

    ``assignments`` has the native ``assignments`` and ``epochs`` mappings.
    ``custody_rows`` is an iterable of accepted terminal measurement records.
    ``load_result(reference)`` must verify the exact whole-file pin before
    returning the saved aggregate RESULT. It is called once per physical run.
    Existing evaluation labels are checked against custody for every threshold.
    Other types use their own native admissible labels and missingness.

    Shared controls count once in physical summaries and retain every original
    contrast membership. Epochs retain all original tasks and assigned draws.
    Only descriptive fields are returned; inputs are never mutated.
    """
    kinds = _selection(awareness_types, TYPES, 'invalid_awareness_selection')
    levels = _selection(thresholds, THRESHOLDS, 'invalid_threshold_selection')
    _require(isinstance(assignments, Mapping), 'assignments_required')
    contrasts = assignments.get('assignments')
    epochs = assignments.get('epochs')
    _require(isinstance(contrasts, list) and contrasts and isinstance(epochs, Mapping)
             and epochs, 'fixed_assignments_required')
    _require(all(isinstance(row, Mapping) and isinstance(row.get('factor'), str)
                 and row['factor'] for row in contrasts), 'invalid_contrast')
    factors = [row['factor'] for row in contrasts]
    _require(len(set(factors)) == len(factors), 'duplicate_contrast')
    expected = assignments.get('expected_factors', factors)
    _require(list(expected) == factors, 'frozen_factor_order_changed')
    runs = set()
    for row in contrasts:
        _require(isinstance(row.get('members'), Mapping) and row['members'],
                 'contrast_members_required')
        for task, arms in row['members'].items():
            _require(isinstance(task, str) and task and isinstance(arms, Mapping)
                     and set(arms) == {'treatment', 'control'}, 'invalid_task_arms')
            for ids in arms.values():
                _require(isinstance(ids, list) and ids
                         and all(isinstance(rid, str) and rid for rid in ids),
                         'invalid_assigned_runs')
                runs.update(ids)
    _require(set(epochs) == runs and all(isinstance(x, str) and x for x in epochs.values()),
             'exact_epoch_membership_required')
    epoch_names = list(dict.fromkeys(epochs.values()))
    by_run = {rid: {} for rid in epochs}
    readout_levels = {'reader_' + level: level for level in THRESHOLDS}
    for row in custody_rows:
        _require(isinstance(row, Mapping), 'invalid_custody_row')
        name = row.get('readout')
        if name not in readout_levels:
            continue
        rid, level = row.get('run_id'), readout_levels[name]
        _require(rid in by_run and level not in by_run[rid], 'duplicate_or_foreign_reader_slot')
        _require(row.get('epoch') == epochs[rid]
                 and row.get('status') in ('observed', 'missing')
                 and isinstance(row.get('evidence'), Mapping), 'invalid_terminal_reader_slot')
        _require((row['status'] == 'observed' and row.get('label') in READER_LABELS)
                 or (row['status'] == 'missing' and row.get('label') is None
                     and isinstance(row.get('reason'), str) and row['reason']),
                 'invalid_reader_label_or_missingness')
        by_run[rid][level] = row
    _require(all(set(rows) == set(THRESHOLDS) for rows in by_run.values()),
             'incomplete_fixed_reader_slots')
    labels = {(kind, level): {} for kind in kinds for level in levels}
    physical_values = {(kind, level): [] for kind in kinds for level in levels}
    references, unavailable = [], 0
    for rid, rows in by_run.items():
        refs = [rows[level]['evidence'].get('result') for level in THRESHOLDS]
        encoded_refs = [json.dumps(ref, sort_keys=True, separators=(',', ':'), allow_nan=False)
                        for ref in refs]
        _require(len(set(encoded_refs)) == 1, 'reader_result_reference_disagreement')
        ref = refs[0]
        if ref is None:
            _require(all(row['status'] == 'missing' for row in rows.values()),
                     'observed_reader_without_result')
            unavailable += 1
            saved = None
        else:
            _require(isinstance(ref, Mapping) and set(ref) == {'path', 'bytes', 'sha256'}
                     and isinstance(ref['path'], str) and ref['path']
                     and type(ref['bytes']) is int and ref['bytes'] > 0
                     and isinstance(ref['sha256'], str) and len(ref['sha256']) == 64,
                     'exact_reader_result_pin_required')
            saved = load_result(ref)
            _require(isinstance(saved, Mapping) and saved.get('schema') == 'caus.reader_context.v1'
                     and saved.get('independent_instrument_calls') is True
                     and saved.get('threshold_results_inferred_from_other_calls') is False
                     and isinstance(saved.get('instruments'), Mapping)
                     and set(saved['instruments']) == set(THRESHOLDS), 'invalid_saved_reader_result')
            references.append(dict(ref))
        for level in THRESHOLDS:
            native = saved['instruments'][level] if saved is not None else None
            _require(saved is None or isinstance(native, Mapping), 'terminal_instrument_record_required')
            if native is not None:
                _require(native.get('instrument') == level
                         and native.get('status') in ('validated', 'validated_partial_observation',
                                                       'technical_missing'), 'invalid_instrument_result')
                if native['status'] in ('validated', 'validated_partial_observation'):
                    _require(isinstance(native.get('judgments'), Mapping)
                             and set(native['judgments']) == set(TYPES), 'all_awareness_judgments_required')
                    for judgment in native['judgments'].values():
                        _require(isinstance(judgment, Mapping) and judgment.get('label') in READER_LABELS
                                 and type(judgment.get('technical_missing_reasoning')) is bool,
                                 'invalid_saved_judgment')
                        admitted = (None if judgment['technical_missing_reasoning']
                                    and judgment['label'] != 'yes' else judgment['label'])
                        _require(judgment.get('admissible_label') == admitted,
                                 'native_admissibility_changed')
                else:
                    _require(native.get('judgments') is None, 'technical_failure_with_judgments')
                evaluation = reader_label(native)
                _require((native['status'] in ('validated', 'validated_partial_observation'))
                         == (evaluation is not None), 'native_evaluation_status_disagreement')
                original_reason = (None if evaluation is not None else
                                   native.get('technical_reason', 'native_reader_technical_missing'))
                _require(rows[level].get('reason') == original_reason,
                         'evaluation_custody_reason_disagreement')
            _require(reader_label(native) == rows[level]['label'], 'evaluation_custody_disagreement')
            if level not in levels:
                continue
            for kind in kinds:
                label = reader_label(native, kind)
                if label is not None:
                    disposition = 'observed'
                elif native is None:
                    disposition = rows[level]['reason']
                elif native['status'] == 'technical_missing':
                    disposition = native.get('technical_reason', 'native_reader_technical_missing')
                else:
                    disposition = 'missing_generated_actor_reasoning'
                _require(isinstance(disposition, str) and disposition, 'missing_disposition_required')
                labels[kind, level][rid] = label
                physical_values[kind, level].append({
                    'label': READER_NAMES[label] if label is not None else None,
                    'status': disposition, 'identity': (rid, 'early', 'reader', kind, level)})
    pooled, split, physical = [], [], []
    for kind in kinds:
        for level in levels:
            current = labels[kind, level]
            physical.append({'awareness_type': kind, 'threshold': level,
                             **distribution(physical_values[kind, level])})
            for assignment in contrasts:
                pooled.append(_descriptive(assignment, current, kind, level, len(contrasts)))
                for epoch in epoch_names:
                    subset = {'factor': assignment['factor'], 'members': {
                        task: {arm: [rid for rid in ids if epochs[rid] == epoch]
                               for arm, ids in arms.items()}
                        for task, arms in assignment['members'].items()}}
                    split.append(_descriptive(subset, current, kind, level, len(contrasts), epoch))
    _require(len({ref['path'] for ref in references}) == len(references),
             'reader_result_reused_for_distinct_runs')
    return {'pooled': pooled, 'epochs': split, 'physical': physical,
            'provenance': {'physical_runs': len(runs), 'reader_slots': 3 * len(runs),
                           'result_files': len(references), 'unavailable_contexts': unavailable,
                           'epoch_counts': dict(Counter(epochs.values())),
                           'source_result_refs_sha256': hashlib.sha256(json.dumps(
                               references, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                           'awareness_types': list(kinds), 'thresholds': list(levels),
                           'factors': factors, 'evaluation_matches_custody': True,
                           'inference_performed': False}}
