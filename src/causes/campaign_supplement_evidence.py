"""Opt-in byte storage for supplement observations; no observation collection.

Raw file pins remain the default. Compact captures must be reconstructed before
the existing admission/history checks. A caller still owns freshness, transport
failure preservation, storage reservations and the complete source closure.
"""

import hashlib
import json
from pathlib import Path

from shared import evidence_bytes as exact


TRANSPORT_SCHEMA = 'causes.supplement_transport_bytes.v1'
CAPTURE_SCHEMA = 'causes.supplement_grouped_capture.v1'
REFERENCE_SCHEMA = 'causes.supplement_grouped_capture_reference.v1'
GROUP_SIZE = 64


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode()


def _object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, 'duplicate_metadata_key')
        value[key] = item
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object,
                      parse_constant=lambda unused: _require(False, 'nonfinite_metadata'))


def _digest(raw):
    return dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def _same(left, right, reason):
    _require(_encoded(left) == _encoded(right), reason)


def _identity(value):
    _require(type(value) is dict and value, 'observation_identity_required')
    _encoded(value)
    return value


def _raw_pin(reference):
    return type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}


def _pin_shape(reference, limit):
    _require(_raw_pin(reference) and type(reference['path']) is str and
             type(reference['bytes']) is int and 0 <= reference['bytes'] <= limit and
             type(reference['sha256']) is str and len(reference['sha256']) == 64 and
             all(c in '0123456789abcdef' for c in reference['sha256']), 'capture_pin_shape')
    path = Path(reference['path'])
    _require(path.is_absolute() and path.resolve() == path and not path.is_symlink()
             and reference['path'] == str(path), 'capture_pin_canonical_path')


def _write(path, raw, limit):
    path = Path(path)
    _require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
             'noncanonical_destination')
    _require(type(raw) is bytes and type(limit) is int and
             0 < limit <= exact.ABSOLUTE_LIMIT and len(raw) <= limit, 'raw_size_limit')
    with path.open('xb') as stream:
        _require(stream.write(raw) == len(raw), 'incomplete_write')
    reference = exact.pin(path, limit=limit)
    _require(reference == dict(path=str(path), **_digest(raw)), 'written_bytes_changed')
    return reference


def write_transport(path, raw, *, identity, encoding='raw', limit=exact.DEFAULT_LIMIT):
    """Retain original INTENT/stdout/stderr bytes without parsing or normalizing.

    The path is the actual stored path, including any caller-chosen gzip suffix.
    This does not run a subprocess or manage its raw spool or failure receipt.
    """
    _identity(identity)
    _require(encoding in ('raw', 'gzip'), 'unsupported_transport_encoding')
    if encoding == 'raw':
        return _write(path, raw, limit)
    value = dict(schema=TRANSPORT_SCHEMA, identity=_json(_encoded(identity)),
                 payload=exact.write_gzip(path, raw, limit=limit))
    _require(read_transport(value, identity=identity, limit=limit) == raw,
             'transport_roundtrip_changed')
    return value


def read_transport(reference, *, identity, limit=exact.DEFAULT_LIMIT):
    """Return verified full raw bytes; untagged pins are never codec-guessed."""
    _identity(identity)
    if _raw_pin(reference):
        return exact.read_pinned(reference, limit=limit)
    _require(type(reference) is dict and set(reference) == {'schema', 'identity', 'payload'}
             and reference['schema'] == TRANSPORT_SCHEMA, 'transport_reference_schema')
    _same(reference['identity'], identity, 'transport_identity_changed')
    return exact.read_gzip(reference['payload'], limit=limit)


def observation_identity(value):
    result = {key: value[key] for key in ('identity', 'observed_from', 'observed_until')}
    _identity(result['identity'])
    _require(all(type(result[key]) is str and result[key]
                 for key in ('observed_from', 'observed_until')), 'capture_observation_time')
    return result


def _domain(scope):
    _require(scope['mode'] == 'live_prefix' and type(scope['id']) is str and scope['id'],
             'compact_capture_live_scope')
    _identity(scope['identity'])
    return dict(id=scope['id'], identity=scope['identity'], baseline=scope['baseline'])


def _capture_reference(reference):
    _require(type(reference) is dict and set(reference) == {'schema', 'wrapper'}
             and reference['schema'] == REFERENCE_SCHEMA and _raw_pin(reference['wrapper']),
             'capture_reference_schema')
    _pin_shape(reference['wrapper'], exact.MANIFEST_LIMIT)


def _wrapper(reference, scope, limit):
    _capture_reference(reference)
    value = _json(exact.read_pinned(reference['wrapper'], limit=exact.MANIFEST_LIMIT))
    _require(type(value) is dict and set(value) ==
             {'schema', 'domain', 'observation', 'ordinal', 'group', 'manifest', 'previous', 'raw'}
             and value['schema'] == CAPTURE_SCHEMA, 'capture_wrapper_schema')
    _same(value['domain'], _domain(scope), 'capture_domain_changed')
    _same(value['observation'], observation_identity(value['observation']),
          'capture_observation_schema')
    ordinal = value['ordinal']
    _require(type(ordinal) is int and ordinal > 0 and type(value['group']) is int and
             value['group'] == (ordinal - 1) // GROUP_SIZE, 'capture_ordinal_or_group')
    _require((value['previous'] is None) == (ordinal == 1), 'capture_initial_predecessor')
    if value['previous'] is not None:
        _capture_reference(value['previous'])
    manifest = _json(exact.read_pinned(value['manifest'], limit=exact.MANIFEST_LIMIT))
    _require(type(manifest) is dict and set(manifest) ==
             {'schema', 'identity', 'baseline', 'segments', 'raw', 'previous'} and
             manifest['schema'] == 'shared.exact_prefix_capture.v1', 'capture_manifest_schema')
    _same(manifest['identity'], dict(domain=value['domain'], group=value['group']),
          'capture_group_identity_changed')
    _same(manifest['baseline'], scope['baseline'], 'capture_baseline_changed')
    _same(manifest['raw'], value['raw'], 'capture_raw_identity_changed')
    _require(type(manifest['segments']) is list and
             len(manifest['segments']) == (ordinal - 1) % GROUP_SIZE + 1,
             'capture_group_length')
    _require((manifest['previous'] is None) == ((ordinal - 1) % GROUP_SIZE == 0),
             'capture_group_predecessor_shape')
    if manifest['previous'] is not None:
        _pin_shape(manifest['previous'], exact.MANIFEST_LIMIT)
    digest = value['raw']
    _require(type(limit) is int and 0 < limit <= exact.ABSOLUTE_LIMIT and
             type(digest) is dict and set(digest) == {'bytes', 'sha256'} and
             type(digest['bytes']) is int and 0 <= digest['bytes'] <= limit and
             type(digest['sha256']) is str and len(digest['sha256']) == 64 and
             all(c in '0123456789abcdef' for c in digest['sha256']), 'capture_raw_digest')
    return value, manifest


def read_capture(scope, reference, observation, *, limit=exact.DEFAULT_LIMIT):
    """Decode a donor for the unchanged full-byte native history validator.

    The immediate predecessor is pinned and its whole raw digest is compared
    with the current prefix, including across groups. Older accepted custody is
    retained by the caller; decoding does not recursively traverse all history.
    """
    if _raw_pin(reference):
        return exact.read_pinned(reference, limit=limit)
    value, manifest = _wrapper(reference, scope, limit)
    _same(value['observation'], observation_identity(observation),
          'capture_observation_changed')
    raw = exact.read_prefix_capture(value['manifest'],
        identity=dict(domain=value['domain'], group=value['group']), limit=limit)
    _same(_digest(raw), value['raw'], 'capture_reconstruction_changed')
    if value['previous'] is None:
        _require(value['ordinal'] == 1 and manifest['previous'] is None,
                 'capture_initial_predecessor')
    else:
        old, old_manifest = _wrapper(value['previous'], scope, limit)
        _require(value['ordinal'] == old['ordinal'] + 1, 'capture_predecessor_ordinal')
        _require(_encoded(value['observation']) != _encoded(old['observation']),
                 'capture_repeated_observation')
        _same(_digest(raw[:old['raw']['bytes']]), old['raw'], 'capture_previous_whole_prefix')
        if value['group'] == old['group']:
            _same(manifest['previous'], old['manifest'], 'capture_detached_predecessor')
        else:
            _require(value['group'] == old['group'] + 1 and old['ordinal'] % GROUP_SIZE == 0
                     and len(old_manifest['segments']) == GROUP_SIZE and
                     manifest['previous'] is None, 'capture_group_rollover')
    return raw


def write_capture(directory, raw, *, scope, observation, previous=None,
                  limit=exact.DEFAULT_LIMIT):
    """Write one opt-in grouped capture, retaining every observation separately."""
    domain = _domain(scope)
    observed = observation_identity(observation)
    _require(type(raw) is bytes and type(limit) is int and
             0 < limit <= exact.ABSOLUTE_LIMIT and len(raw) <= limit, 'raw_size_limit')
    ordinal, parent_manifest = 1, None
    if previous is not None:
        parent, unused = _wrapper(previous, scope, limit)
        before = read_capture(scope, previous, parent['observation'], limit=limit)
        _require(raw.startswith(before), 'capture_previous_whole_prefix')
        _require(_encoded(observed) != _encoded(parent['observation']),
                 'capture_repeated_observation')
        ordinal = parent['ordinal'] + 1
        if parent['ordinal'] % GROUP_SIZE:
            parent_manifest = parent['manifest']
    group = (ordinal - 1) // GROUP_SIZE
    # Validate baseline and scope before creating a new destination.
    base = exact.read_pinned(scope['baseline'], limit=limit)
    _require(raw.startswith(base), 'capture_baseline_prefix')
    directory = Path(directory)
    _require(directory.is_absolute() and directory.resolve() == directory
             and not directory.is_symlink(), 'noncanonical_destination')
    directory.mkdir()
    manifest = exact.write_prefix_capture(directory / 'prefix', raw,
        baseline=scope['baseline'], identity=dict(domain=domain, group=group),
        previous=parent_manifest, limit=limit)
    value = dict(schema=CAPTURE_SCHEMA, domain=domain, observation=observed,
                 ordinal=ordinal, group=group, manifest=manifest, previous=previous, raw=_digest(raw))
    ref = _write(directory / 'OBSERVATION_CAPTURE.json', _encoded(value) + b'\n',
                 exact.MANIFEST_LIMIT)
    result = dict(schema=REFERENCE_SCHEMA, wrapper=ref)
    _require(read_capture(scope, result, observation, limit=limit) == raw,
             'capture_roundtrip_changed')
    return result


PAIR_SCHEMA = 'causes.supplement_transport_pair.v1'
STREAM_SCHEMA = 'causes.supplement_transport_stream.v1'


def _pair(reference, limit):
    _require(type(reference) is dict and set(reference) == {'schema', 'manifest', 'streams'}
             and reference['schema'] == PAIR_SCHEMA, 'transport_pair_schema')
    _pin_shape(reference['manifest'], exact.MANIFEST_LIMIT)
    streams = reference['streams']
    _require(type(streams) is dict and set(streams) == {'stdout', 'stderr'},
             'transport_pair_streams')
    for stream, digest in streams.items():
        exact._digest_shape(digest, min(limit, 1024**2) if stream == 'stderr' else limit)
    _require(sum(row['bytes'] for row in streams.values()) <= limit,
             'transport_pair_total_limit')
    return streams


def read_transport_pair(reference, *, identity, domain, limit=32 * 1024**2):
    """Recover both exact streams from one bounded, source-pinned transport record."""
    from shared import evidence_delta as delta
    _identity(identity)
    _identity(domain)
    streams = _pair(reference, limit)
    raw = delta.read_transport(reference['manifest'],
        identity=dict(transport=identity, streams=streams), domain=domain, limit=limit)
    boundary = streams['stdout']['bytes']
    _require(len(raw) == boundary + streams['stderr']['bytes'], 'transport_pair_length')
    result = dict(stdout=raw[:boundary], stderr=raw[boundary:])
    for stream, data in result.items():
        _same(_digest(data), streams[stream], 'transport_pair_stream_changed')
    return result


def write_transport_pair(directory, stdout, stderr, *, identity, domain,
                         previous=None, limit=32 * 1024**2):
    """Retain both streams using two files; current identity never comes from a prior cut.

    The caller must persist INTENT before collection. Partial streams are ordinary
    exact bytes here; only the caller's complete and return-code checks qualify them.
    """
    from shared import evidence_delta as delta
    _identity(identity)
    _identity(domain)
    _require(type(stdout) is bytes and type(stderr) is bytes and
             type(limit) is int and 0 < limit <= delta.RAW_LIMIT and
             len(stdout) + len(stderr) <= limit and len(stderr) <= 1024**2,
             'transport_pair_input_limit')
    streams = dict(stdout=_digest(stdout), stderr=_digest(stderr))
    prior = None
    if previous is not None:
        _pair(previous, limit)
        old = _json(exact.read_pinned(previous['manifest'], limit=exact.MANIFEST_LIMIT))
        _require(type(old['identity']) is dict and set(old['identity']) == {'transport', 'streams'},
                 'transport_pair_previous_identity')
        _same(old['identity']['streams'], previous['streams'], 'transport_pair_previous_streams')
        _require(_encoded(old['identity']['transport']) != _encoded(identity),
                 'transport_pair_repeated_identity')
        read_transport_pair(previous, identity=old['identity']['transport'], domain=domain, limit=limit)
        prior = previous['manifest']
    manifest = delta.write_transport(directory, stdout + stderr,
        identity=dict(transport=identity, streams=streams), domain=domain,
        previous=prior, limit=limit)
    reference = dict(schema=PAIR_SCHEMA, manifest=manifest, streams=streams)
    recovered = read_transport_pair(reference, identity=identity, domain=domain, limit=limit)
    _require(recovered == dict(stdout=stdout, stderr=stderr), 'transport_pair_roundtrip')
    return reference


def transport_stream_reference(pair, stream, *, limit=32 * 1024**2):
    """Create a typed view without writing another file."""
    _pair(pair, limit)
    _require(stream in ('stdout', 'stderr'), 'transport_stream_name')
    return dict(schema=STREAM_SCHEMA, pair=_json(_encoded(pair)), stream=stream)


def read_transport_stream(reference, *, identity, domain=None, limit=32 * 1024**2):
    """Read old raw/gzip streams or an explicitly identified new pair view."""
    if not (type(reference) is dict and reference.get('schema') == STREAM_SCHEMA):
        return read_transport(reference, identity=identity, limit=limit)
    _require(set(reference) == {'schema', 'pair', 'stream'} and
             reference['stream'] in ('stdout', 'stderr'), 'transport_stream_schema')
    _identity(identity)
    _require(identity.get('stream') == reference['stream'], 'transport_stream_identity')
    transport = {key: value for key, value in identity.items() if key != 'stream'}
    return read_transport_pair(reference['pair'], identity=transport, domain=domain,
                               limit=limit)[reference['stream']]
