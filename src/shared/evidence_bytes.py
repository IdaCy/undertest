"""Lossless, explicitly typed storage for immutable captured bytes.

These helpers neither collect observations nor interpret journal events. Callers
must bind capture identity and pass recovered bytes to their existing validators.
Old files are never replaced, removed, or implicitly converted.
"""

import gzip
import hashlib
import json
from pathlib import Path
import re
import stat
import zlib


DEFAULT_LIMIT = 128 * 1024**2
ABSOLUTE_LIMIT = 512 * 1024**2
MANIFEST_LIMIT = 8 * 1024**2
SEGMENT_LIMIT = 16384


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _size(value, maximum):
    _require(type(value) is int and 0 <= value <= maximum, 'invalid_byte_count')
    return value


def _limit(value):
    _require(type(value) is int and 0 < value <= ABSOLUTE_LIMIT, 'invalid_size_limit')
    return value


def _canonical(path):
    path = Path(path)
    _require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
             'noncanonical_path')
    return path


def _digest(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _digest_shape(value, maximum):
    _require(type(value) is dict and set(value) == {'bytes', 'sha256'}, 'invalid_digest')
    _size(value['bytes'], maximum)
    _require(type(value['sha256']) is str and
             re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None, 'invalid_sha256')


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          ValueError('nonfinite_json_constant:' + value)))


def _identity(value):
    _require(type(value) is dict and value, 'capture_identity_required')
    return _encoded(value)


def pin(path, *, limit=DEFAULT_LIMIT):
    """Pin one canonical regular file with a bounded read."""
    limit = _limit(limit)
    path = _canonical(path)
    _require(stat.S_ISREG(path.stat().st_mode), 'regular_file_required')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, 'file_size_limit')
    return {'path': str(path), **_digest(raw)}


def _pin_shape(reference, limit):
    _require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'},
             'invalid_file_pin')
    _digest_shape({k: reference[k] for k in ('bytes', 'sha256')}, limit)
    _require(type(reference['path']) is str, 'invalid_file_path')
    path = _canonical(reference['path'])
    _require(reference['path'] == str(path), 'nonliteral_canonical_path')
    return path


def read_pinned(reference, *, limit=DEFAULT_LIMIT):
    """Verify the exact stored bytes; this does not guess their encoding."""
    limit = _limit(limit)
    path = _pin_shape(reference, limit)
    _require(stat.S_ISREG(path.stat().st_mode), 'regular_file_required')
    with path.open('rb') as stream:
        raw = stream.read(reference['bytes'] + 1)
    _require(_digest(raw) == {k: reference[k] for k in ('bytes', 'sha256')},
             'stored_bytes_changed')
    return raw


def _write(path, raw):
    path = _canonical(path)
    with path.open('xb') as stream:
        _require(stream.write(raw) == len(raw), 'incomplete_write')
    reference = pin(path, limit=ABSOLUTE_LIMIT)
    _require(reference == {'path': str(path), **_digest(raw)}, 'written_bytes_changed')
    return reference


def write_gzip(path, raw, *, limit=DEFAULT_LIMIT):
    """Exclusively retain deterministic gzip bytes and their raw identity."""
    limit = _limit(limit)
    _require(type(raw) is bytes and len(raw) <= limit, 'raw_size_limit')
    packed = gzip.compress(raw, compresslevel=6, mtime=0)
    _require(len(packed) <= ABSOLUTE_LIMIT, 'stored_size_limit')
    return {'schema': 'shared.exact_gzip_bytes.v1', 'stored': _write(path, packed),
            'raw': _digest(raw)}


def read_gzip(record, *, limit=DEFAULT_LIMIT):
    """Recover one complete gzip member, rejecting excess output or trailing data."""
    limit = _limit(limit)
    _require(type(record) is dict and set(record) == {'schema', 'stored', 'raw'} and
             record['schema'] == 'shared.exact_gzip_bytes.v1', 'gzip_record_schema')
    _digest_shape(record['raw'], limit)
    packed = read_pinned(record['stored'], limit=ABSOLUTE_LIMIT)
    decoder = zlib.decompressobj(wbits=31)
    raw = decoder.decompress(packed, record['raw']['bytes'] + 1)
    _require(len(raw) <= record['raw']['bytes'], 'decoded_size_limit')
    _require(decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data,
             'incomplete_or_trailing_gzip')
    _require(_digest(raw) == record['raw'], 'decoded_bytes_changed')
    return raw


def _manifest(reference, identity):
    value = _json(read_pinned(reference, limit=MANIFEST_LIMIT))
    _require(type(value) is dict and set(value) ==
             {'schema', 'identity', 'baseline', 'segments', 'raw', 'previous'},
             'prefix_manifest_fields')
    _require(value['schema'] == 'shared.exact_prefix_capture.v1', 'prefix_manifest_schema')
    _require(_identity(value['identity']) == _identity(identity), 'capture_identity_changed')
    _require(type(value['segments']) is list and len(value['segments']) <= SEGMENT_LIMIT,
             'segment_count_limit')
    if value['previous'] is not None:
        _pin_shape(value['previous'], MANIFEST_LIMIT)
        _require(len(value['segments']) >= 2, 'continued_capture_segment_count')
    else:
        _require(len(value['segments']) == 1, 'initial_capture_segment_count')
    return value


def read_prefix_capture(reference, *, identity, limit=DEFAULT_LIMIT):
    """Reconstruct a flat pinned prefix/suffix capture before native interpretation."""
    limit = _limit(limit)
    value = _manifest(reference, identity)
    _digest_shape(value['raw'], limit)
    baseline = read_pinned(value['baseline'], limit=limit)
    raw = bytearray(baseline)
    paths = {value['baseline']['path']}
    for row in value['segments']:
        _require(type(row) is dict and set(row) == {'offset', 'file'}, 'segment_fields')
        _size(row['offset'], limit)
        _require(row['offset'] == len(raw), 'segment_gap_or_overlap')
        suffix = read_pinned(row['file'], limit=limit)
        _require(row['file']['path'] not in paths, 'repeated_segment_path')
        paths.add(row['file']['path'])
        _require(len(raw) + len(suffix) <= value['raw']['bytes'], 'capture_size_limit')
        raw.extend(suffix)
    _require(_digest(raw) == value['raw'], 'capture_bytes_changed')
    if value['previous'] is not None:
        # Verify the immediate provenance link without recursively loading ancestry.
        parent = _manifest(value['previous'], identity)
        _digest_shape(parent['raw'], limit)
        _require(_encoded(parent['baseline']) == _encoded(value['baseline']) and
                 _encoded(value['segments'][:len(parent['segments'])]) ==
                 _encoded(parent['segments']) and
                 len(value['segments']) == len(parent['segments']) + 1 and
                 value['segments'][-1]['offset'] == parent['raw']['bytes'] and
                 _digest(raw[:parent['raw']['bytes']]) == parent['raw'],
                 'capture_predecessor_changed')
    return bytes(raw)


def write_prefix_capture(directory, raw, *, baseline, identity, previous=None,
                         limit=DEFAULT_LIMIT):
    """Retain only a new exact suffix; every observation gets its own manifest.

    Identity must distinguish the source journal's domain from any composite
    history domain. The existing full byte validators remain caller obligations.
    """
    limit = _limit(limit)
    _require(type(raw) is bytes and len(raw) <= limit, 'raw_size_limit')
    _identity(identity)
    base = read_pinned(baseline, limit=limit)
    if previous is None:
        before, segments = base, []
    else:
        before = read_prefix_capture(previous, identity=identity, limit=limit)
        old = _manifest(previous, identity)
        _require(_encoded(old['baseline']) == _encoded(baseline), 'capture_baseline_changed')
        segments = old['segments']
    _require(raw.startswith(before), 'capture_prefix_changed')
    _require(len(segments) < SEGMENT_LIMIT, 'segment_count_limit')
    directory = _canonical(directory)
    # Construct and bound the complete metadata before creating a destination.
    suffix_path = directory / 'SUFFIX.bytes'
    suffix = raw[len(before):]
    value = {'schema': 'shared.exact_prefix_capture.v1',
             'identity': _json(_identity(identity)), 'baseline': baseline,
             'segments': segments + [{'offset': len(before),
                                      'file': {'path': str(suffix_path), **_digest(suffix)}}],
             'raw': _digest(raw), 'previous': previous}
    manifest = _encoded(value) + b'\n'
    _require(len(manifest) <= MANIFEST_LIMIT, 'manifest_size_limit')
    directory.mkdir()
    _write(suffix_path, suffix)
    return _write(directory / 'CAPTURE.json', manifest)
