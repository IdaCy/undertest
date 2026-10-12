from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _checked_json(reference, record):
    _require(isinstance(reference, Mapping)
             and set(reference) == {'path', 'bytes', 'sha256'}
             and isinstance(reference['path'], str)
             and type(reference['bytes']) is int and reference['bytes'] > 0
             and isinstance(reference['sha256'], str), 'exact_measurement_pin_required')
    raw = record(reference, read=True)
    _require(type(raw) is bytes and len(raw) == reference['bytes']
             and hashlib.sha256(raw).hexdigest() == reference['sha256'],
             'accepted_measurement_pin_changed')

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'duplicate_measurement_json_key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('nonfinite_measurement_json')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _accepted_result(result_reference, review_reference, *, assignments_reference,
                     source_reference, acceptance_flag, record):
    _require(isinstance(acceptance_flag, str) and acceptance_flag.startswith('accepted_'),
             'measurement_acceptance_flag_required')
    result = _checked_json(result_reference, record)
    review = _checked_json(review_reference, record)
    _require(review.get(acceptance_flag) is True and review.get('actual') == result_reference
             and review.get('source') == result.get('source') == source_reference
             and review.get('assignments') == result.get('assignments') == assignments_reference
             and review.get('outputs') == result.get('outputs')
             and review.get('manifest') == result.get('manifest'),
             'accepted_measurement_base_join')
    return result


def load_accepted_manifest(result_reference, review_reference, *, assignments_reference,
                           source_reference, acceptance_flag, record):
    """Load accepted metadata for slot selection without opening measurement labels."""
    result = _accepted_result(result_reference, review_reference,
                              assignments_reference=assignments_reference,
                              source_reference=source_reference,
                              acceptance_flag=acceptance_flag, record=record)
    manifest = _checked_json(result['manifest'], record)
    _require(manifest.get('assignments') == assignments_reference,
             'accepted_manifest_assignment_changed')
    return manifest


def load_accepted_measurements(result_reference, review_reference, *, assignments_reference,
                               source_reference, acceptance_flag, record):
    """Load a whole-pinned, independently accepted custody table without raw evidence reads."""
    result = _accepted_result(result_reference, review_reference,
                              assignments_reference=assignments_reference,
                              source_reference=source_reference,
                              acceptance_flag=acceptance_flag, record=record)
    _require(isinstance(result.get('outputs'), dict)
             and 'MEASUREMENT_CUSTODY' in result['outputs'], 'measurement_custody_required')
    rows = _checked_json(result['outputs']['MEASUREMENT_CUSTODY'], record)
    _require(type(rows) is list and type(result.get('physical_measurements')) is int
             and result['physical_measurements'] == len(rows), 'accepted_measurement_count')
    return rows


def measurement_keys(rows, *, epochs, readout_names, require_epoch=False):
    """Validate terminal slots, retaining failed and unavailable measurements as occupied."""
    _require(isinstance(epochs, Mapping) and epochs
             and all(isinstance(k, str) and k and isinstance(v, str) and v
                     for k, v in epochs.items()), 'frozen_measurement_epochs_required')
    _require(isinstance(readout_names, Sequence) and not isinstance(readout_names, (str, bytes))
             and readout_names and all(isinstance(x, str) and x for x in readout_names)
             and len(set(readout_names)) == len(readout_names),
             'frozen_measurement_readouts_required')
    _require(type(rows) is list, 'measurement_rows_required')
    keys = set()
    for row in rows:
        _require(isinstance(row, Mapping), 'invalid_measurement_row')
        key = row.get('run_id'), row.get('readout')
        _require(all(isinstance(x, str) for x in key)
                 and key[0] in epochs and key[1] in readout_names,
                 'measurement_outside_frozen_assignment')
        _require(key not in keys, 'duplicate_physical_measurement')
        _require(row.get('status') in ('observed', 'missing')
                 and isinstance(row.get('evidence'), Mapping), 'terminal_measurement_required')
        if row['status'] == 'missing':
            _require(row.get('label') is None and isinstance(row.get('reason'), str)
                     and row['reason'].strip(), 'explicit_missing_measurement_required')
        else:
            _require(isinstance(row.get('label'), str) and row['label'],
                     'observed_measurement_label_required')
        _require((not require_epoch or 'epoch' in row)
                 and ('epoch' not in row or row['epoch'] == epochs[key[0]]),
                 'measurement_epoch_changed')
        keys.add(key)
    return keys


class MeasurementGroupSelector:
    """Select an ordered stream using one snapshot of assignments and occupied slots.

    Construct once per pass, then call ``admit`` before enumerating each group's
    payloads. Observed and missing base slots are equally occupied. Duplicate
    groups refuse even when fully occupied; any ValueError aborts the pass.
    """

    def __init__(self, base_keys, *, epochs, readout_names):
        _require(type(base_keys) is set, 'measurement_groups_required')
        _require(isinstance(epochs, Mapping), 'frozen_measurement_epochs_required')
        epochs = dict(epochs)
        _require(epochs and all(isinstance(k, str) and k
                                and isinstance(v, str) and v for k, v in epochs.items()),
                 'frozen_measurement_epochs_required')
        _require(isinstance(readout_names, Sequence)
                 and not isinstance(readout_names, (str, bytes)),
                 'frozen_measurement_readouts_required')
        readout_names = tuple(readout_names)
        _require(readout_names and all(isinstance(x, str) and x for x in readout_names)
                 and len(set(readout_names)) == len(readout_names),
                 'frozen_measurement_readouts_required')
        self._runs = frozenset(epochs)
        self._readouts = frozenset(readout_names)
        self._base = frozenset(base_keys)
        _require(all(self._assigned(key) for key in self._base),
                 'base_measurement_keys_outside_assignment')
        self._seen = set()

    def _assigned(self, key):
        return (type(key) is tuple and len(key) == 2
                and all(isinstance(x, str) for x in key)
                and key[0] in self._runs and key[1] in self._readouts)

    def admit(self, declared):
        """Return whether all declared slots are new; refuse invalid or mixed groups."""
        _require(type(declared) in (list, tuple) and declared,
                 'explicit_measurement_group_keys_required')
        _require(all(self._assigned(key) for key in declared),
                 'measurement_group_outside_assignment')
        keys = set(declared)
        _require(len(keys) == len(declared) and not keys.intersection(self._seen),
                 'duplicate_measurement_group')
        self._seen.update(keys)
        overlap = keys.intersection(self._base)
        _require(not overlap or overlap == keys, 'partial_measurement_group_overlap')
        return not overlap


def select_new_measurement_groups(groups, base_keys, *, group_keys, epochs, readout_names):
    """Filter metadata groups before payload enumeration; partial overlaps always refuse."""
    _require(type(groups) is list and type(base_keys) is set, 'measurement_groups_required')
    selector = MeasurementGroupSelector(base_keys, epochs=epochs, readout_names=readout_names)
    selected = []
    for group in groups:
        if selector.admit(group_keys(group)):
            selected.append(group)
    return selected


def merge_measurements(base_rows, new_rows, *, epochs, readout_names):
    """Append disjoint terminal slots without modifying or replacing accepted records."""
    base = measurement_keys(base_rows, epochs=epochs, readout_names=readout_names,
                            require_epoch=True)
    new = measurement_keys(new_rows, epochs=epochs, readout_names=readout_names)
    _require(not base.intersection(new), 'existing_measurement_cannot_be_replaced')
    return copy.deepcopy(base_rows + new_rows)
