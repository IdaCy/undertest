"""Opt-in exact transport storage against a nonrecursive full-byte anchor.

No observations are collected or interpreted. Callers bind fresh identities,
stable reuse domains, accepted predecessor pins, source closure and full-write
budgets. Every referenced file must remain retained. Existing raw/gzip formats
are unchanged; this distinct manifest must be decoded before native validation.
"""

import gzip
from pathlib import Path

from shared import evidence_bytes as exact


SCHEMA = 'shared.anchored_transport_bytes.v1'
RAW_LIMIT = 32 * 1024**2
MANIFEST_LIMIT = 1024**2
STORED_LIMIT = RAW_LIMIT + 64 * 1024
BLOCK = 16 * 1024
GROUP_SIZE = 64
OP_LIMIT = 2 * (RAW_LIMIT // BLOCK) + 1
FIELDS = {'schema', 'identity', 'domain', 'ordinal', 'group', 'previous',
          'encoding', 'anchor', 'payload', 'operations', 'raw'}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _limit(limit):
    _require(type(limit) is int and 0 < limit <= RAW_LIMIT, 'delta_size_limit')
    return limit


def _same(left, right, reason):
    _require(exact._encoded(left) == exact._encoded(right), reason)


def _record(reference, domain, limit):
    value = exact._json(exact.read_pinned(reference, limit=MANIFEST_LIMIT))
    _require(type(value) is dict and set(value) == FIELDS and
             value['schema'] == SCHEMA, 'delta_manifest_schema')
    exact._identity(value['identity'])
    exact._identity(value['domain'])
    _same(value['domain'], domain, 'delta_domain_changed')
    ordinal = value['ordinal']
    _require(type(ordinal) is int and ordinal > 0 and type(value['group']) is int and
             value['group'] == (ordinal - 1) // GROUP_SIZE, 'delta_ordinal_or_group')
    _require((value['previous'] is None) == (ordinal == 1), 'delta_previous_shape')
    if value['previous'] is not None:
        exact._pin_shape(value['previous'], MANIFEST_LIMIT)
    exact._digest_shape(value['raw'], limit)
    payload = value['payload']
    _require(type(payload) is dict and set(payload) == {'schema', 'stored', 'raw'} and
             payload['schema'] == 'shared.exact_gzip_bytes.v1', 'delta_payload_schema')
    exact._digest_shape(payload['raw'], limit)
    exact._pin_shape(payload['stored'], STORED_LIMIT)
    _require(value['encoding'] in ('gzip', 'block_delta'), 'delta_encoding')
    operations = value['operations']
    _require(type(operations) is list and len(operations) <= OP_LIMIT,
             'delta_operation_limit')
    if value['encoding'] == 'gzip':
        _require(value['anchor'] is None and not operations, 'delta_full_shape')
        _same(value['payload']['raw'], value['raw'], 'delta_full_raw_changed')
    else:
        exact._pin_shape(value['anchor'], MANIFEST_LIMIT)
        _require(ordinal % GROUP_SIZE != 1, 'delta_group_requires_full_anchor')
    return value


def _previous(value, domain, limit):
    if value['previous'] is None:
        return None
    old = _record(value['previous'], domain, limit)
    _require(old['ordinal'] + 1 == value['ordinal'], 'delta_previous_ordinal')
    _require(exact._encoded(old['identity']) != exact._encoded(value['identity']),
             'delta_repeated_identity')
    return old


def read_transport(reference, *, identity, domain, limit=RAW_LIMIT):
    """Reconstruct bytes from one full anchor, never a chain.

    Allow memory for the anchor, literals and output (up to three raw limits),
    plus compressed inputs and interpreter overhead. No file is deleted.
    """
    limit = _limit(limit)
    exact._identity(identity)
    exact._identity(domain)
    value = _record(reference, domain, limit)
    _same(value['identity'], identity, 'delta_identity_changed')
    previous = _previous(value, domain, limit)
    literal = exact.read_gzip(value['payload'], limit=limit)
    if value['encoding'] == 'gzip':
        return literal
    _require(previous is not None and previous['group'] == value['group'],
             'delta_anchor_group')
    expected = value['previous'] if previous['encoding'] == 'gzip' else previous['anchor']
    _same(value['anchor'], expected, 'delta_detached_anchor')
    anchor = _record(value['anchor'], domain, limit)
    _require(anchor['encoding'] == 'gzip' and anchor['group'] == value['group'] and
             anchor['ordinal'] < value['ordinal'], 'delta_nonrecursive_anchor_required')
    before = exact.read_gzip(anchor['payload'], limit=limit)
    output = bytearray()
    literal_offset = 0
    for operation in value['operations']:
        _require(type(operation) is list and len(operation) == 3,
                 'delta_operation_shape')
        kind, offset, length = operation
        _require(kind in ('copy', 'literal') and type(offset) is int and offset >= 0
                 and type(length) is int and length > 0, 'delta_operation_range')
        source = before if kind == 'copy' else literal
        _require(offset <= len(source) and length <= len(source) - offset and
                 length <= value['raw']['bytes'] - len(output), 'delta_span_bounds')
        if kind == 'literal':
            _require(offset == literal_offset, 'delta_literal_order')
            literal_offset += length
        output.extend(source[offset:offset + length])
    _require(literal_offset == len(literal), 'delta_unused_literal_bytes')
    _same(exact._digest(output), value['raw'], 'delta_reconstructed_bytes_changed')
    return bytes(output)


def _blocks(before, raw):
    """Bounded exact matching; optimization quality cannot affect byte integrity."""
    matches = []
    for offset in range(0, len(before) - BLOCK + 1, BLOCK):
        start = raw.find(before[offset:offset + BLOCK])
        if start >= 0:
            matches.append((start, start + BLOCK, offset))
    chosen = []
    end = 0
    for start, stop, offset in sorted(matches):
        if start < end:
            continue
        if (chosen and chosen[-1][1] == start and
                chosen[-1][2] + chosen[-1][1] - chosen[-1][0] == offset):
            chosen[-1] = (chosen[-1][0], stop, chosen[-1][2])
        else:
            chosen.append((start, stop, offset))
        end = stop
    operations, literal = [], bytearray()
    end = 0
    for start, stop, offset in chosen:
        if start > end:
            operations.append(['literal', len(literal), start - end])
            literal.extend(raw[end:start])
        operations.append(['copy', offset, stop - start])
        end = stop
    if end < len(raw):
        operations.append(['literal', len(literal), len(raw) - end])
        literal.extend(raw[end:])
    _require(len(operations) <= OP_LIMIT, 'delta_operation_limit')
    return operations, bytes(literal)


def _payload(path, raw):
    packed = gzip.compress(raw, compresslevel=6, mtime=0)
    _require(len(packed) <= STORED_LIMIT, 'delta_stored_size_limit')
    return packed, dict(schema='shared.exact_gzip_bytes.v1',
                        stored=dict(path=str(path), **exact._digest(packed)),
                        raw=exact._digest(raw))


def write_transport(directory, raw, *, identity, domain, previous=None, limit=RAW_LIMIT):
    """Exclusively write a full gzip or smaller delta, preserving all raw bytes.

    Selection compares the complete manifest and compressed payload before any
    write. A corrupt predecessor refuses; only size/group choice falls back to
    gzip. The full raw bound and caller's worst-case reservation still apply.
    """
    limit = _limit(limit)
    _require(type(raw) is bytes and len(raw) <= limit, 'delta_raw_size_limit')
    identity = exact._json(exact._identity(identity))
    domain = exact._json(exact._identity(domain))
    directory = exact._canonical(directory)
    payload_path = directory / 'PAYLOAD.gz'
    ordinal, anchor_reference, before = 1, None, None
    if previous is not None:
        old = _record(previous, domain, limit)
        # A previous manifest pin alone is insufficient: rehash its exact bytes.
        recovered = read_transport(previous, identity=old['identity'], domain=domain, limit=limit)
        _require(exact._encoded(old['identity']) != exact._encoded(identity),
                 'delta_repeated_identity')
        ordinal = old['ordinal'] + 1
        if (ordinal - 1) % GROUP_SIZE:
            anchor_reference = previous if old['encoding'] == 'gzip' else old['anchor']
            anchor = _record(anchor_reference, domain, limit)
            _require(anchor['encoding'] == 'gzip', 'delta_nonrecursive_anchor_required')
            before = recovered if old['encoding'] == 'gzip' else exact.read_gzip(anchor['payload'], limit=limit)
    packed, payload = _payload(payload_path, raw)
    value = dict(schema=SCHEMA, identity=identity, domain=domain, ordinal=ordinal,
                 group=(ordinal - 1) // GROUP_SIZE, previous=previous, encoding='gzip',
                 anchor=None, payload=payload, operations=[], raw=exact._digest(raw))
    encoded = exact._encoded(value) + b'\n'
    if before is not None:
        operations, literal = _blocks(before, raw)
        delta_packed, delta_payload = _payload(payload_path, literal)
        candidate = dict(value, encoding='block_delta', anchor=anchor_reference,
                         payload=delta_payload, operations=operations)
        delta_encoded = exact._encoded(candidate) + b'\n'
        if (len(delta_encoded) <= MANIFEST_LIMIT and
                len(delta_encoded) + len(delta_packed) < len(encoded) + len(packed)):
            value, encoded, packed = candidate, delta_encoded, delta_packed
    _require(len(encoded) <= MANIFEST_LIMIT, 'delta_manifest_size_limit')
    directory.mkdir()
    _same(exact._write(payload_path, packed), value['payload']['stored'],
          'delta_stored_payload_changed')
    reference = exact._write(directory / 'TRANSPORT.json', encoded)
    _require(read_transport(reference, identity=identity, domain=domain, limit=limit) == raw,
             'delta_roundtrip_changed')
    return reference
