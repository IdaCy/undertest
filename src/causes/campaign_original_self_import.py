"""Invoke accepted original-self batch helpers over immutable saved evidence only."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

from causes import campaign_original_self_batch as batch
from causes import campaign_selected_self_feed as feed


SCHEMA = 'causes.original_self_import_bindings.v1'
NATIVE_NAMES = {'reader', 'api', 'runner', 'evidence', 'labels', 'materializer', 'segments'}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'canonical_path')
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
    return dict(path=str(path), bytes=size, sha256=digest.hexdigest())


class Inputs:
    """Track every declared input for the native read guard and final rehash."""

    def __init__(self):
        self.references = {}

    def record(self, reference, read=False):
        require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}
                and type(reference['bytes']) is int and reference['bytes'] >= 0,
                'exact_pin_required')
        require(pin(reference['path']) == reference, 'changed_input')
        previous = self.references.setdefault(reference['path'], dict(reference))
        require(previous == reference, 'conflicting_input')
        return Path(reference['path']).read_bytes() if read else None

    def checked(self, reference):
        def pairs(items):
            value = {}
            for key, item in items:
                require(key not in value, 'duplicate_json_key')
                value[key] = item
            return value

        def invalid(_):
            raise ValueError('nonfinite_json')
        return json.loads(self.record(reference, read=True), object_pairs_hook=pairs,
                          parse_constant=invalid)

    def accepted(self, reference, flag, **joins):
        require(type(flag) is str and flag.startswith('accepted_'), 'acceptance_flag')
        peer = self.checked(reference)
        require(peer.get(flag) is True and peer.get('findings') == []
                and all(encoded(peer.get(key)) == encoded(value) for key, value in joins.items()), flag)
        return peer


def module(reference, name, inputs):
    inputs.record(reference)
    spec = importlib.util.spec_from_file_location('original_self_import_' + name, reference['path'])
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    require(pin(value.__file__) == reference, 'native_source_changed')
    return value


def occupied_keys(bindings, assignments, segments, inputs):
    """Check every existing segment with the accepted public segment loader."""
    previous = bindings['previous']
    result = inputs.checked(previous['result'])
    inputs.accepted(previous['review'], previous['acceptance_flag'], actual=previous['result'],
                    **{key: result[key] for key in
                       ('source', 'manifest', 'coverage', 'assignments', 'public_merge_source')})
    inputs.record(result['public_merge_source'])
    require(result['assignments'] == bindings['assignments']
            and all(result['public_merge_source'][key] == bindings['sources']['reuse'][key]
                    for key in ('bytes', 'sha256')), 'previous_identity')
    manifest = inputs.checked(result['manifest'])
    descriptors = bindings['previous_segments']
    require(type(descriptors) is list and descriptors and descriptors[0]['kind'] == 'custody'
            and all(row['kind'] != 'custody' for row in descriptors[1:])
            and encoded([row['segment'] for row in descriptors]) == encoded(manifest['segments'])
            and manifest['assignments'] == bindings['assignments'], 'previous_segments_changed')
    inputs.record(result['source'])
    original_record = segments.record
    keys = set()
    try:
        segments.record = inputs.record
        reuse = segments.load_reuse(bindings['sources']['reuse'])
        for descriptor in descriptors:
            rows, current, _ = segments.load_segment(descriptor, reuse,
                bindings['assignments'], assignments, new=False)
            require(not keys & current and len(rows) == len(current), 'previous_duplicate_slot')
            keys.update(current)
    finally:
        segments.record = original_record
    require(len(keys) == result['terminal_slots'] == manifest['physical_measurements'],
            'previous_occupied_count')
    return keys


def derive(bindings_reference, inputs=None):
    """Prepare finite native rows; incomplete paired copies remain pending."""
    inputs = inputs or Inputs()
    read = inputs.checked
    bindings = read(bindings_reference)
    require(bindings.get('schema') == SCHEMA and bindings['source'] == pin(__file__), 'bindings_schema')
    sources = bindings['sources']
    require(set(sources) == NATIVE_NAMES | {'batch', 'feed', 'reader_feed', 'reuse', 'snapshot'},
            'native_source_roster')
    expected = {'batch': batch, 'feed': feed, 'reader_feed': feed.reader,
                'reuse': batch.reuse, 'snapshot': batch.snapshot}
    for key, value in expected.items():
        require(pin(value.__file__) == sources[key], 'public_helper_changed:' + key)
        inputs.record(sources[key])
    inputs.accepted(bindings['batch_source_review'], 'accepted_original_self_batch_source',
                    source=sources['batch'])
    inputs.accepted(bindings['segment_source_review'], 'accepted_measurement_segment_append_source',
                    source=sources['segments'])
    registration = read(bindings['registration'])
    configuration = feed.validate_registration(registration, read)
    selection = read(registration['selection'])
    require(registration['selection'] == bindings['selection']
            and selection['assignments'] == bindings['assignments'], 'fixed_selection_join')
    assignments = read(bindings['assignments'])
    inputs.accepted(bindings['assignment_review'], bindings['assignment_acceptance_flag'],
                    assignments=bindings['assignments'])
    modules = {name: module(sources[name], name, inputs) for name in sorted(NATIVE_NAMES)}
    old_keys = occupied_keys(bindings, assignments, modules['segments'], inputs)
    own = batch.accepted_snapshots(bindings['self_cut'], bindings['self_cut_review'],
        bindings['selection'], registration['configuration'], kind='self', read=read,
        raw=lambda ref: inputs.record(ref, read=True), pin=pin)
    reader_registration = read(registration['reader_registration'])
    other = batch.accepted_snapshots(bindings['reader_cut'], bindings['reader_cut_review'],
        bindings['selection'], reader_registration['configuration'], kind='reader', read=read,
        raw=lambda ref: inputs.record(ref, read=True), pin=pin)
    require(configuration == read(registration['configuration']), 'self_configuration_changed')
    global_jobs = read(selection['global_jobs'])
    ordered = [job['allocation']['run_id'] for job in global_jobs]
    policy = bindings['batch_policy']
    require(set(policy) == {'initial_ids', 'batch_size', 'final'}, 'batch_policy_fields')
    chosen = batch.select_batch(ordered, set(own), set(other), old_keys,
        initial_ids=policy['initial_ids'], batch_size=policy['batch_size'], final=policy['final'])
    selected = chosen['selected_run_ids']
    if not selected:
        return dict(bindings=bindings, inputs=inputs, modules=modules, selection=chosen,
                    old_keys=old_keys, pending=True)
    by_job = {job['allocation']['run_id']: job for job in global_jobs}
    config_by_stage = {(p['stage'], p['role']): p['config']
                       for p in reader_registration['producers']}
    configs = {row['run_id']: config_by_stage[(row['stage'], row['provider_role'])]
               for row in selection['rows'] if row['run_id'] in set(selected)}
    prepared = batch.prepare_groups(selected, by_job, configs, own, other,
        self_cut=bindings['self_cut'], self_peer=bindings['self_cut_review'],
        membership_ref=bindings['terminal_membership'], runner_source=sources['runner'], read=read)
    files = dict(inputs.references)
    for ref in prepared['files']:
        require(ref['path'] not in files or files[ref['path']] == ref, 'prepared_pin_conflict')
        files[ref['path']] = ref
    prepared['files'] = list(files.values())
    return dict(bindings=bindings, inputs=inputs, modules=modules, selection=chosen,
        old_keys=old_keys, pending=False, prepared=prepared, jobs=by_job, assignments=assignments)


def save(path, value):
    with Path(path).open('xb') as stream:
        stream.write(encoded(value) + b'\n')
    return pin(path)


def guarded_bind(state, output, extra_files=()):
    """Use the exact READ17 guard/profile and native self auditor once."""
    require(state['pending'] is False, 'paired_copy_batch_pending')
    output = Path(output)
    require(output.is_absolute() and output.resolve() == output and not output.exists(), 'fresh_output')
    b, m, prepared = state['bindings'], state['modules'], state['prepared']
    sources = [b['source'], *b['sources'].values()]
    guard = m['reader'].ReadGuard(prepared['files'] + list(extra_files), output, sources,
                                 prepared['read_directories'])
    sys.addaudithook(guard.audit)

    def deny(*args, **kwargs):
        raise ValueError('saved_self_import_external_action_forbidden')
    try:
        output.mkdir()
        intent = save(output / 'INTENT.v1.json', dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            source=b['source'], selected_run_ids=state['selection']['selected_run_ids'],
            previous=b['previous'], model_calls=0, host_calls=0))
        with patch.object(subprocess, 'run', deny), patch.object(subprocess, 'Popen', deny), \
             patch.object(socket.socket, 'connect', deny), patch.object(os, 'kill', deny):
            result = batch.bind_batch(prepared['groups'], state['jobs'], state['assignments'],
                reader=m['reader'], api=m['api'], runner=m['runner'], evidence=m['evidence'],
                labels=m['labels'], materializer=m['materializer'], record=guard)
        require(result['selected_run_ids'] == state['selection']['selected_run_ids'], 'selected_output_changed')
        keys = batch.reuse.measurement_keys(result['slots'], epochs=state['assignments']['epochs'],
                    readout_names=state['assignments']['readout_names'], require_epoch=True)
        require(not keys & state['old_keys'], 'occupied_slot_replay')
        guard.finish()
        slots = save(output / 'SLOTS.v1.json', result.pop('slots'))
        return save(output / 'RESULT.v1.json', dict(schema='causes.original_self_import.v1',
            at=datetime.datetime.now(datetime.timezone.utc).isoformat(), source=b['source'],
            batch_source=b['sources']['batch'], assignments=b['assignments'], slots=slots,
            self_cut=b['self_cut'], self_cut_review=b['self_cut_review'], reader_cut=b['reader_cut'],
            reader_cut_review=b['reader_cut_review'], previous=b['previous'], intent=intent,
            existing_occupied_slots=len(state['old_keys']), native_sources=b['sources'],
            raw_captures_copied=False, **result))
    except BaseException as error:
        if output.is_dir():
            save(output / 'FAILED.v1.json', dict(error_type=type(error).__name__, error=str(error),
                                               automatic_retry=False))
        raise
    finally:
        guard.active = False


def run(request_reference, authority_reference):
    inputs = Inputs()
    request, authority = map(inputs.checked, (request_reference, authority_reference))
    source = pin(__file__)
    require(request.get('schema') == 'causes.original_self_import_request.v1'
            and request['source'] == source and authority.get('accepted_original_self_import_execution') is True
            and authority['source'] == source and authority['request'] == request_reference
            and type(authority['maximum_invocations']) is int and authority['maximum_invocations'] == 1
            and authority['automatic_retry'] is False
            and request['runtime_authorized'] is True, 'import_not_authorized')
    inputs.accepted(request['source_review'], 'accepted_original_self_import_source', source=source)
    inputs.accepted(request['bindings_review'], 'accepted_original_self_import_bindings',
                    source=source, bindings=request['bindings'])
    state = derive(request['bindings'], inputs)
    require(not state['pending'], 'paired_copy_batch_pending')
    return guarded_bind(state, request['output'], [request_reference, authority_reference])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--authority', required=True)
    args = parser.parse_args()
    print(json.dumps(run(pin(args.request), pin(args.authority)), sort_keys=True))
