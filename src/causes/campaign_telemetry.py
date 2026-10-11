import base64
import hashlib
import re


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def select_health_series(metrics, *, name_pattern, histogram_families=(),
                         required_names=(), required_label_series=()):
    """Select typed health values from a captured body and its parsed values.

    Label requirements are (counter name, label regex) pairs. Creation gauges
    are returned as epoch identities; callers compare them separately from
    event counts. Histogram counts and buckets are excluded. The caller supplies
    parsed values; the body pin and TYPE metadata are checked here, but sample
    lines are not reparsed to establish their correspondence with those values.
    """
    body = metrics['body']
    raw = base64.b64decode(body['base64'], validate=True)
    _require(len(raw) == body['bytes'] and hashlib.sha256(raw).hexdigest() == body['sha256'],
             'metrics_body_pin')
    types = {}
    for line in raw.decode().splitlines():
        if line.startswith('# TYPE '):
            parts = line.split()
            _require(len(parts) == 4 and parts[2] not in types, 'metric_type_metadata')
            types[parts[2]] = parts[3]
    histogram_families = tuple(histogram_families)
    required_label_series = tuple(required_label_series)
    created_names = {name.removesuffix('_total') + '_created'
                     for name, _ in required_label_series}
    selected = {}
    for value in metrics['values']:
        name, labels, number = value['name'], value['labels'], value['value']
        relevant = re.search(name_pattern, name)
        labeled_counter = any(name == wanted and re.search(pattern, labels)
                              for wanted, pattern in required_label_series)
        if name.endswith('_created') and (relevant or name in created_names):
            family = name[:-8]
            _require(types.get(name) == 'gauge' and
                     (types.get(family + '_total') == 'counter' or types.get(family) == 'histogram'),
                     'unrecognized_health_metric_epoch:' + name)
        elif relevant or labeled_counter:
            if types.get(name) == 'counter' and name.endswith('_total'):
                pass
            else:
                family = next((family for family in histogram_families
                               if name.startswith(family + '_')), None)
                _require(family is not None, 'unrecognized_health_metric_type:' + name)
                _require(types.get(family) == 'histogram', 'health_histogram_type:' + family)
                if name.endswith(('_count', '_bucket')):
                    continue
                _require(name == family + '_sum', 'unrecognized_health_histogram_series:' + name)
        else:
            continue
        key = (name, labels)
        _require(key not in selected, 'duplicate_health_metric_series')
        _require(type(number) in (int, float) and 0 <= number < float('inf'), 'invalid_health_counter')
        selected[key] = number
    for name in required_names:
        _require(any(key[0] == name for key in selected), 'health_counter_coverage_absent:' + name)
    for name, pattern in required_label_series:
        _require(any(key[0] == name and re.search(pattern, key[1]) for key in selected),
                 'health_label_series_coverage_absent:' + name + ':' + str(pattern))
    return selected


def verify_journal_append(value, previous_raw, previous_cursor):
    """Reconstruct a complete journal prefix from a captured append receipt."""
    _require(value['schema'] == 'causes.verified_journal_append.v1'
             and value['cursor_before'] == previous_cursor, 'journal_cursor_changed')
    raw = base64.b64decode(value['base64'], validate=True)
    _require(len(raw) == value['bytes'] and hashlib.sha256(raw).hexdigest() == value['sha256'],
             'journal_delta_pin')
    prefix = b'' if previous_raw is None else previous_raw
    _require(value['offset'] == len(prefix), 'journal_append_offset')
    prefix += raw
    cursor = value['cursor_after']
    _require(len(prefix) == cursor['bytes'] and prefix.endswith(b'\n')
             and hashlib.sha256(prefix).hexdigest() == cursor['sha256'], 'journal_complete_prefix_pin')
    if previous_cursor is not None:
        _require((cursor['device'], cursor['inode']) == (previous_cursor['device'], previous_cursor['inode']),
                 'journal_identity_changed')
    return prefix, cursor
