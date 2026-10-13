"""Append accepted measurement segments by reference, computing coverage only."""
from __future__ import annotations

import argparse
import collections
import copy
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path


SCHEMA = 'causes.measurement_segment_append_bindings.v1'
GROUPS = {
    'reader': ('reader_presence', 'reader_strongly_favors', 'reader_leans_towards_or_favors'),
    'self': ('self_evaluation_presence', 'self_evaluation_strongly_favors',
             'self_evaluation_leans_towards_or_favors'),
}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def exact(left, right):
    return encoded(left) == encoded(right)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
            'canonical_file_required')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def record(reference, read=False):
    require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}
            and type(reference['bytes']) is int and reference['bytes'] > 0,
            'exact_file_pin_required')
    path = Path(reference['path'])
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
            'canonical_file_required')
    body = path.read_bytes()
    require(len(body) == reference['bytes']
            and hashlib.sha256(body).hexdigest() == reference['sha256'], 'file_pin_changed')
    return body if read else None


def checked(reference):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('nonfinite_json')

    return json.loads(record(reference, read=True), object_pairs_hook=pairs,
                      parse_constant=invalid)


def accepted(reference, flag, **joins):
    require(type(flag) is str and flag.startswith('accepted_'), 'acceptance_flag_required')
    peer = checked(reference)
    require(peer.get(flag) is True and peer.get('findings') == []
            and all(exact(peer.get(key), value) for key, value in joins.items()), flag)
    return peer


def load_reuse(reference):
    record(reference)
    spec = importlib.util.spec_from_file_location('accepted_segment_measurement_reuse',
                                                 reference['path'])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(pin(module.__file__) == reference, 'measurement_reuse_source_changed')
    return module


def load_segment(descriptor, reuse, assignments_reference, assignments, *, new):
    require(type(descriptor) is dict
            and set(descriptor) == {'kind', 'epoch_policy', 'segment', 'group'},
            'exact_segment_descriptor_required')
    kind, segment = descriptor['kind'], descriptor['segment']
    require(kind in ('custody', 'slots', 'output_slots')
            and descriptor['epoch_policy'] in ('required', 'optional_match'), 'segment_type')
    require(type(segment) is dict and type(segment.get('physical_measurements')) is int
            and segment['physical_measurements'] > 0, 'segment_count')
    result = checked(segment['result'])
    peer = accepted(segment['review'], segment['acceptance_flag'],
                    actual=segment['result'], source=segment['source'])
    require(result['source'] == segment['source'], 'segment_source_join')
    record(segment['source'])
    if kind == 'custody':
        require(not new and descriptor['epoch_policy'] == 'required', 'base_epoch_required')
        rows = reuse.load_accepted_measurements(segment['result'], segment['review'],
            assignments_reference=segment['original_assignments'],
            source_reference=segment['source'], acceptance_flag=segment['acceptance_flag'],
            record=record)
        require(result['outputs']['MEASUREMENT_CUSTODY'] == segment['payload'], 'custody_payload')
    else:
        if kind == 'slots':
            require(peer['slots'] == result['slots'] == segment['payload'], 'slots_payload')
        else:
            require(peer['outputs'] == result['outputs']
                    and result['outputs']['slots'] == segment['payload'], 'output_slots_payload')
        rows = checked(segment['payload'])
    require(type(rows) is list and len(rows) == segment['physical_measurements'], 'segment_length')
    keys = reuse.measurement_keys(rows, epochs=assignments['epochs'],
        readout_names=assignments['readout_names'],
        require_epoch=descriptor['epoch_policy'] == 'required')
    if new:
        require(kind != 'custody' and result['assignments'] == peer['assignments']
                == assignments_reference, 'new_segment_assignments')
        group = descriptor['group']
        require(type(group) is dict and set(group) == {'kind', 'membership', 'count'}
                and group['kind'] in GROUPS, 'complete_instrument_group_required')
        if group['membership'] == 'terminal_run_ids':
            require(peer['membership'] == result['membership'], 'new_membership_join')
            runs = checked(result['membership'])['terminal_run_ids']
        else:
            require(group['membership'] == 'selected_run_ids', 'new_membership_type')
            runs = result['selected_run_ids']
            require(exact(peer['selected_run_ids'], runs), 'new_selected_runs_join')
        require(type(runs) is list and runs and all(type(run) is str for run in runs)
                and len(runs) == len(set(runs)), 'new_duplicate_or_empty_groups')
        require(group['count'] in ('occupied', 'counts.occupied'), 'new_count_type')
        count = result['occupied'] if group['count'] == 'occupied' else result['counts']['occupied']
        require(type(count) is int and count == len(rows), 'new_occupied_count')
        groups = [[(run, name) for name in GROUPS[group['kind']]] for run in runs]
        require(keys == {key for group_keys in groups for key in group_keys},
                'partial_or_extra_instrument_group')
    else:
        require(descriptor['group'] is None, 'previous_group_must_be_null')
        groups = []
    return rows, keys, groups


def coverage(rows, assignments):
    names, epochs = assignments['readout_names'], assignments['epochs']
    require(set(names) == set(GROUPS['self'] + GROUPS['reader'])
            and assignments['primary_readouts'] == ['self_evaluation_presence', 'reader_presence'],
            'six_instrument_assignment_required')
    tables = {name: {} for name in names}
    for row in rows:
        tables[row['readout']][row['run_id']] = row
    counts = {}
    for name, table in tables.items():
        count = collections.Counter(row['status'] for row in table.values())
        counts[name] = dict(observed=count['observed'], missing=count['missing'],
                            pending=len(epochs) - len(table))
    comparisons = []
    factors = set()
    for item in assignments['assignments']:
        require(item['factor'] not in factors and item['members'], 'duplicate_or_empty_contrast')
        factors.add(item['factor'])
        runs = {run for arms in item['members'].values() for arm in arms.values() for run in arm}
        require(runs and runs <= set(epochs), 'contrast_outside_assignments')
        detail = {}
        for name, table in tables.items():
            count = collections.Counter(table[run]['status'] if run in table else 'pending'
                                        for run in runs)
            detail[name] = {status: count[status] for status in ('observed', 'missing', 'pending')}
        comparisons.append(dict(contrast=item['factor'], tasks=len(item['members']),
            physical_runs=len(runs), counts=detail,
            primary_complete=all(detail[name]['pending'] == 0 for name in assignments['primary_readouts'])))
    return dict(physical_runs_assigned=len(epochs), physical_slots_assigned=len(epochs) * len(names),
        terminal_slots=len(rows), pending_slots=sum(value['pending'] for value in counts.values()),
        counts=counts, comparisons=comparisons)


def rows_digest(rows):
    digest = hashlib.sha256()
    for row in rows:
        body = encoded(row)
        digest.update(len(body).to_bytes(8, 'big'))
        digest.update(body)
    return digest.hexdigest()


def derive(bindings_reference):
    bindings = checked(bindings_reference)
    require(bindings.get('schema') == SCHEMA, 'append_bindings_schema')
    assignments = checked(bindings['assignments'])
    accepted(bindings['assignment_review'], bindings['assignment_acceptance_flag'],
             assignments=bindings['assignments'])
    previous = bindings['previous']
    result = checked(previous['result'])
    accepted(previous['review'], previous['acceptance_flag'], actual=previous['result'],
             **{key: result[key] for key in
                ('source', 'manifest', 'coverage', 'assignments', 'public_merge_source')})
    record(result['source'])
    require(result['assignments'] == bindings['assignments']
            and result['public_merge_source'] == bindings['public_merge_source'], 'previous_sources')
    reuse = load_reuse(bindings['public_merge_source'])
    manifest, old_coverage = checked(result['manifest']), checked(result['coverage'])
    descriptors = bindings['previous_segments']
    require(type(descriptors) is list and descriptors
            and exact([item['segment'] for item in descriptors], manifest['segments'])
            and descriptors[0]['kind'] == 'custody'
            and all(item['kind'] != 'custody' for item in descriptors[1:]), 'previous_segments_changed')
    require(manifest['assignments'] == bindings['assignments'], 'manifest_assignments')
    old_rows, base_rows = [], None
    for descriptor in descriptors:
        rows, _, _ = load_segment(descriptor, reuse, bindings['assignments'], assignments, new=False)
        if base_rows is None:
            base_rows = rows
        old_rows.extend(rows)
    old_keys = reuse.measurement_keys(old_rows, epochs=assignments['epochs'],
                                     readout_names=assignments['readout_names'])
    require(len(old_rows) == result['terminal_slots'] == manifest['physical_measurements'],
            'previous_terminal_count')
    original_coverage = coverage(old_rows, assignments)
    require(all(exact(old_coverage.get(key), value) for key, value in original_coverage.items())
            and result['pending_slots'] == original_coverage['pending_slots'], 'previous_coverage_changed')
    additions = bindings['additions']
    require(type(additions) is list and additions, 'new_segments_required')
    selector = reuse.MeasurementGroupSelector(old_keys, epochs=assignments['epochs'],
                                              readout_names=assignments['readout_names'])
    new_rows = []
    for descriptor in additions:
        rows, _, groups = load_segment(descriptor, reuse, bindings['assignments'], assignments, new=True)
        for group in groups:
            require(selector.admit(group), 'occupied_measurement_group_cannot_be_replayed')
        new_rows.extend(rows)
    old_digest, new_digest = rows_digest(old_rows), rows_digest(new_rows)
    merged = reuse.merge_measurements(base_rows, old_rows[len(base_rows):] + new_rows,
                                     epochs=assignments['epochs'], readout_names=assignments['readout_names'])
    require(rows_digest(merged[:len(old_rows)]) == old_digest
            and rows_digest(merged[len(old_rows):]) == new_digest, 'literal_rows_changed')
    current = coverage(merged, assignments)
    next_manifest = copy.deepcopy(manifest)
    next_manifest['segments'].extend(copy.deepcopy(item['segment']) for item in additions)
    next_manifest['physical_measurements'] = len(merged)
    next_manifest['all_primary_terminal'] = all(item['primary_complete'] for item in current['comparisons'])
    require(exact(next_manifest['segments'][:len(descriptors)], manifest['segments']), 'old_segments_changed')
    facts = dict(original_slots_retained=len(old_rows), added_slots=len(new_rows),
        added_status_counts=dict(collections.Counter(row['status'] for row in new_rows)),
        previous_segments=len(descriptors), added_segments=len(additions),
        old_rows_sha256=old_digest, new_rows_sha256=new_digest,
        digest_encoding='sha256 of eight-byte big-endian length followed by canonical JSON for each row')
    return bindings, result, manifest, next_manifest, current, facts


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
    return pin(path)


def run(request_reference):
    request = checked(request_reference)
    require(request.get('schema') == 'causes.measurement_segment_append_request.v1'
            and request['source'] == pin(__file__) and request['root_authorized'] is True
            and type(request['maximum_invocations']) is int and request['maximum_invocations'] == 1
            and request['automatic_retry'] is False
            and all(type(request[key]) is int and request[key] == 0
                    for key in ('model_calls', 'host_calls', 'payload_bytes_copied')),
            'append_request_not_authorized')
    accepted(request['source_review'], 'accepted_measurement_segment_append_source', source=request['source'])
    accepted(request['bindings_review'], 'accepted_measurement_segment_append_bindings',
             source=request['source'], source_review=request['source_review'], bindings=request['bindings'])
    bindings, prior, old_manifest, manifest, counts, facts = derive(request['bindings'])
    output = Path(request['output'])
    require(output.is_absolute() and output.resolve() == output and not output.exists(), 'fresh_output_required')
    output.mkdir()
    at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    intent = save(output / 'INTENT.v1.json', dict(request=request_reference, source=request['source'], at=at))
    coverage_ref = save(output / 'COVERAGE.v1.json', dict(schema='causes.measurement_segment_coverage.v1',
        as_of=at, **counts, effects_computed=False, inference_released=False, selection_changed=False))
    manifest['coverage'] = coverage_ref
    manifest_ref = save(output / 'MANIFEST.v1.json', manifest)
    changes = [dict(field=key, before=old_manifest.get(key), after=value)
               for key, value in manifest.items() if not exact(old_manifest.get(key), value)]
    require({item['field'] for item in changes} <=
            {'coverage', 'segments', 'physical_measurements', 'all_primary_terminal'}, 'manifest_fields_changed')
    inverse = copy.deepcopy(manifest)
    for change in changes:
        if change['field'] in old_manifest:
            inverse[change['field']] = change['before']
        else:
            del inverse[change['field']]
    require(exact(inverse, old_manifest), 'manifest_inverse_changed')
    derivation = save(output / 'MANIFEST_DERIVATION.v1.json', dict(parent=prior['manifest'],
        result=manifest_ref, changes=changes, whole_inverse_equal=True,
        previous_segments_literal=True, **facts, payload_bytes_copied=0))
    return save(output / 'RESULT.v1.json', dict(schema='causes.measurement_segments.v1', at=at,
        source=request['source'], request=request_reference, bindings=request['bindings'], intent=intent,
        previous=bindings['previous']['result'], previous_review=bindings['previous']['review'],
        assignments=bindings['assignments'], assignment_review=bindings['assignment_review'],
        public_merge_source=bindings['public_merge_source'], manifest=manifest_ref,
        manifest_derivation=derivation, coverage=coverage_ref, **facts,
        terminal_slots=counts['terminal_slots'], pending_slots=counts['pending_slots'],
        primary_complete_contrasts=sum(item['primary_complete'] for item in counts['comparisons']),
        analysis_performed=False, effects_computed=False, inference_released=False,
        payload_bytes_copied=0, host_calls=0, model_calls=0))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    print(json.dumps(run(pin(args.request)), sort_keys=True))
