"""Select exact source ranges for immutable staging without interpreting content."""

import base64
import hashlib


ANCHOR_BYTES = 65536
STRIDE_BYTES = 1048576
MAX_ANCHORS = 128
MAX_SOURCE_RANGES = 8


def _pinned(reference, data, max_bytes):
    if (not isinstance(reference, dict) or set(reference) != {'path', 'bytes', 'sha256'}
            or not isinstance(reference['path'], str)
            or type(reference['bytes']) is not int or not 0 <= reference['bytes'] <= max_bytes
            or not isinstance(reference['sha256'], str)
            or len(reference['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in reference['sha256'])
            or type(data) is not bytes or len(data) != reference['bytes']
            or hashlib.sha256(data).hexdigest() != reference['sha256']):
        raise ValueError('invalid_range_selection_input')


def _right(a, b, i, j):
    limit = min(len(a) - i, len(b) - j)
    matched = 0
    while matched + STRIDE_BYTES <= limit:
        end = matched + STRIDE_BYTES
        if a[i + matched:i + end] != b[j + matched:j + end]:
            break
        matched = end
    low, high = 0, min(STRIDE_BYTES, limit - matched)
    while low < high:
        middle = (low + high + 1) // 2
        if a[i + matched:i + matched + middle] == b[j + matched:j + matched + middle]:
            low = middle
        else:
            high = middle - 1
    return matched + low


def _left(a, b, i, j):
    limit = min(i, j)
    matched = 0
    while matched + STRIDE_BYTES <= limit:
        end = matched + STRIDE_BYTES
        if a[i - end:i - matched] != b[j - end:j - matched]:
            break
        matched = end
    low, high = 0, min(STRIDE_BYTES, limit - matched)
    while low < high:
        middle = (low + high + 1) // 2
        if a[i - matched - middle:i - matched] == b[j - matched - middle:j - matched]:
            low = middle
        else:
            high = middle - 1
    return matched + low


def select_segments(source_reference, source_bytes, target_reference, target_bytes,
                    *, max_bytes=1024 ** 3):
    """Return an exact segmented encoding, or None for a normal payload fallback.

    Complete supplied byte strings must match their pins. At most 128 sparse
    anchors identify ranges, without parsing either input. Up to eight ranges
    are selected; every range is compared and the full reconstruction hashed.
    Selection is deterministic but need not find the smallest representation.

    This pure helper does not establish source-path availability or freshness.
    The caller binds permitted source pins; the stager checks filesystem custody.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError('invalid_range_selection_limit')
    _pinned(source_reference, source_bytes, max_bytes)
    _pinned(target_reference, target_bytes, max_bytes)
    a, b = source_bytes, target_bytes
    candidates = []

    def add(source_offset, target_offset, length):
        if length >= ANCHOR_BYTES:
            if a[source_offset:source_offset + length] != b[target_offset:target_offset + length]:
                raise ValueError('range_selection_mismatch')
            row = (source_offset, target_offset, length)
            if row not in candidates:
                candidates.append(row)

    add(0, 0, _right(a, b, 0, 0))
    suffix = _left(a, b, len(a), len(b))
    add(len(a) - suffix, len(b) - suffix, suffix)
    stride = max(STRIDE_BYTES, (len(a) + MAX_ANCHORS - 1) // MAX_ANCHORS)
    for offset in range(0, max(0, len(a) - ANCHOR_BYTES + 1), stride):
        if any(start <= offset and offset + ANCHOR_BYTES <= start + length
               for start, _, length in candidates):
            continue
        found = b.find(a[offset:offset + ANCHOR_BYTES])
        if found < 0:
            continue
        left = _left(a, b, offset, found)
        right = _right(a, b, offset, found)
        add(offset - left, found - left, left + right)

    selected = []
    for row in sorted(candidates, key=lambda x: (-x[2], x[1], x[0])):
        if all(row[1] + row[2] <= old[1] or old[1] + old[2] <= row[1]
               for old in selected):
            selected.append(row)
            if len(selected) == MAX_SOURCE_RANGES:
                break
    if not selected:
        return None
    selected.sort(key=lambda x: x[1])
    segments, position, reused = [], 0, 0
    digest = hashlib.sha256()

    def literal(data):
        if data:
            segments.append({'base64': base64.b64encode(data).decode('ascii'), 'bytes': len(data)})
            digest.update(data)

    for offset, target_offset, length in selected:
        literal(b[position:target_offset])
        copied = a[offset:offset + length]
        if copied != b[target_offset:target_offset + length]:
            raise ValueError('range_selection_mismatch')
        segments.append({'source': dict(source_reference), 'offset': offset, 'bytes': length})
        digest.update(copied)
        reused += length
        position = target_offset + length
    literal(b[position:])
    if (not 1 <= len(segments) <= 2 * MAX_SOURCE_RANGES + 1 <= 32
            or sum(row['bytes'] for row in segments) != target_reference['bytes']
            or digest.hexdigest() != target_reference['sha256']):
        raise ValueError('range_selection_reconstruction_mismatch')
    return {'segments': segments, 'reused_bytes': reused,
            'literal_bytes': len(b) - reused, 'source_range_count': len(selected)}
