"""Bind saved derived views to distinct, immutable native reader requests.

This module checks bytes and measurement identity. Callers separately establish
the scientific selection and acceptance of each view's derivation. It performs
no writes, API calls, history resets, or authorization of execution.
"""
import hashlib
import json
from pathlib import Path

from causes import campaign_reader as reader
from causes import measurement_view


ROW_SCHEMA = 'causes.derived_reader_input.v1'
PROTOCOL_SCHEMA = 'causes.derived_reader_protocol.v1'


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
            'derived_reader_canonical_file_required')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def raw(ref):
    require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
            and type(ref['bytes']) is int and ref['bytes'] >= 0,
            'derived_reader_pin_invalid')
    path = Path(ref['path'])
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
            'derived_reader_canonical_file_required')
    body = path.read_bytes()
    require(len(body) == ref['bytes'] and hashlib.sha256(body).hexdigest() == ref['sha256'],
            'derived_reader_input_changed')
    return body


def checked(ref):
    return json.loads(raw(ref))


def read_view(reference):
    """Select an existing view from a pinned JSON file without duplicating it."""
    require(type(reference) is dict and set(reference) == {'file', 'selector'}
            and type(reference['selector']) is list
            and all(type(key) is str and key for key in reference['selector']),
            'derived_reader_view_reference_invalid')
    value = checked(reference['file'])
    for key in reference['selector']:
        require(type(value) is dict and key in value, 'derived_reader_view_selector_missing')
        value = value[key]
    measurement_view.prepare_judge_context(value)
    require((value['phase'] == 'prefix' and value['prefix_context'] is None)
            or (value['phase'] == 'full' and value['prefix_context'] is not None),
            'derived_reader_paired_prefix_required')
    if value['prefix_context'] is not None:
        reader.prepare_context(value['prefix_context'], len(value['initial_context']),
                               'prefix', value['initial_context'])
    return value


def protocol_identity(protocol):
    require(type(protocol) is dict and set(protocol) == {
        'schema', 'checker_config', 'prompts', 'prompts_directory', 'reader_source'}
        and protocol['schema'] == PROTOCOL_SCHEMA, 'derived_reader_protocol_invalid')
    config = checked(protocol['checker_config'])
    raw(protocol['reader_source'])
    loaded = pin(reader.__file__)
    require(all(loaded[key] == protocol['reader_source'][key]
                for key in ('bytes', 'sha256')) and loaded['sha256'] == reader._SOURCE_SHA256,
            'derived_reader_native_source_changed')
    require(set(protocol['prompts']) == {'rubric', *reader.INSTRUMENTS},
            'derived_reader_instrument_set_changed')
    prompt_hashes = {}
    directory = Path(protocol['prompts_directory'])
    for name, ref in protocol['prompts'].items():
        require(Path(ref['path']) == directory / (name + '.txt'),
                'derived_reader_prompt_path_changed')
        prompt_hashes[name] = hashlib.sha256(raw(ref)).hexdigest()
    return dict(config_sha256=reader.digest(config), prompts_sha256=prompt_hashes,
                reader_source_sha256=loaded['sha256'],
                instruments=[dict(name=name, schema_sha256=reader.digest(reader.schema(name)))
                             for name in reader.INSTRUMENTS])


def measurement_key(run_id, view_kind, stage, view_sha256, protocol_sha256):
    require(type(run_id) is str and run_id and type(view_kind) is str and view_kind
            and stage in ('early', 'end'), 'derived_reader_identity_invalid')
    for value in (view_sha256, protocol_sha256):
        require(value is None or (type(value) is str and len(value) == 64
                and all(c in '0123456789abcdef' for c in value)),
                'derived_reader_identity_hash_invalid')
    require(protocol_sha256 is not None, 'derived_reader_protocol_identity_required')
    return 'derived_reader_' + reader.digest(dict(run_id=run_id, view_kind=view_kind,
        stage=stage, view_sha256=view_sha256, protocol_sha256=protocol_sha256))


def _evidence(provenance):
    require(type(provenance) is list and provenance, 'derived_reader_provenance_required')
    for ref in provenance:
        raw(ref)


def build_row(run_id, view_kind, stage, reference, provenance, protocol):
    """Prepare all three payload identities; private provenance stays outside them."""
    identity = protocol_identity(protocol)
    _evidence(provenance)
    view = read_view(reference)
    require(view['phase'] == {'early': 'prefix', 'end': 'full'}.get(stage),
            'derived_reader_stage_changed')
    context, targets = measurement_view.prepare_judge_context(view)
    config = checked(protocol['checker_config'])
    payloads = {}
    for instrument in reader.INSTRUMENTS:
        payload, pins = reader.make_payload(config, protocol['prompts_directory'], context, instrument)
        require(pins == dict(config_sha256=identity['config_sha256'],
                rubric_sha256=identity['prompts_sha256']['rubric'],
                threshold_sha256=identity['prompts_sha256'][instrument],
                schema_sha256=reader.digest(reader.schema(instrument))),
                'derived_reader_payload_protocol_changed')
        body = reader.encoded(payload)
        payloads[instrument] = dict(canonical_sha256=reader.digest(payload), body_bytes=len(body),
            file_sha256=hashlib.sha256(body + b'\n').hexdigest(), pins=pins)
    require(protocol_identity(protocol) == identity, 'derived_reader_protocol_changed_during_preparation')
    view_hash, protocol_hash = reader.digest(view), reader.digest(identity)
    return dict(schema=ROW_SCHEMA, run_id=run_id, view_kind=view_kind, stage=stage,
        measurement_key=measurement_key(run_id, view_kind, stage, view_hash, protocol_hash),
        disposition='prepared', view=reference, provenance=provenance,
        view_canonical_sha256=view_hash, protocol_sha256=protocol_hash,
        context_canonical_sha256=reader.digest(context), targets_sha256=reader.digest(targets),
        payloads=payloads, runtime_authorized=False)


def unavailable_row(run_id, view_kind, stage, reason, provenance, protocol):
    """Keep an assigned missing arm explicit without synthesizing a request."""
    require(type(reason) is str and reason, 'derived_reader_missing_reason_required')
    identity = protocol_identity(protocol)
    _evidence(provenance)
    protocol_hash = reader.digest(identity)
    return dict(schema=ROW_SCHEMA, run_id=run_id, view_kind=view_kind, stage=stage,
        measurement_key=measurement_key(run_id, view_kind, stage, None, protocol_hash),
        disposition='unavailable', reason=reason, provenance=provenance,
        protocol_sha256=protocol_hash, view=None, payloads={}, runtime_authorized=False)


def prepare_reader(row, prep, config, modules):
    """Rebuild the frozen row before supplying only its four-key native view."""
    require(modules['campaign_reader'] is reader, 'derived_reader_module_mismatch')
    protocol = prep['protocol']
    require(reader.encoded(config) == reader.encoded(checked(protocol['checker_config']))
            and prep['prompts_directory'] == protocol['prompts_directory'],
            'derived_reader_runtime_protocol_changed')
    require(row.get('schema') == ROW_SCHEMA and row.get('disposition') == 'prepared',
            'derived_reader_prepared_row_required')
    rebuilt = build_row(row['run_id'], row['view_kind'], row['stage'], row['view'],
                        row['provenance'], protocol)
    require(reader.encoded(rebuilt) == reader.encoded(row), 'derived_reader_row_changed')
    return read_view(row['view'])


def validate_membership(assignments, rows, protocol):
    """Verify the entire ordered assignment list, including unavailable arms."""
    require(type(assignments) is list and assignments and type(rows) is list
            and len(assignments) == len(rows), 'derived_reader_assignment_count_changed')
    seen = set()
    counts = dict(assigned=len(rows), ready=0, unavailable=0, requests=0)
    for assignment, row in zip(assignments, rows):
        require(type(assignment) is dict and set(assignment) == {'run_id', 'view_kind', 'stage'}
                and all(row.get(key) == value for key, value in assignment.items()),
                'derived_reader_assignment_order_changed')
        identity = tuple(assignment[key] for key in ('run_id', 'view_kind', 'stage'))
        require(identity not in seen, 'derived_reader_duplicate_assignment')
        seen.add(identity)
        if row.get('disposition') == 'prepared':
            rebuilt = build_row(**assignment, reference=row['view'],
                                provenance=row['provenance'], protocol=protocol)
            counts['ready'] += 1
            counts['requests'] += len(reader.INSTRUMENTS)
        else:
            require(row.get('disposition') == 'unavailable', 'derived_reader_disposition_invalid')
            rebuilt = unavailable_row(**assignment, reason=row['reason'],
                                      provenance=row['provenance'], protocol=protocol)
            counts['unavailable'] += 1
        require(reader.encoded(rebuilt) == reader.encoded(row), 'derived_reader_row_changed')
    require(len({row['measurement_key'] for row in rows}) == len(rows),
            'derived_reader_duplicate_measurement')
    return counts
