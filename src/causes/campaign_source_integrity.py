"""Verify an exact union of immutable pins once at one synchronous boundary.

Callers must supply every required roster. This module does not infer missing
dependencies, grant admission, or replace dynamic guards. Each call reads all
unique files again: an entry check and a later exit check are separate calls.
There is no cache, retained file content, or cross-thread verification state.
"""

import hashlib
from pathlib import Path
import re


class SourceIntegrityError(RuntimeError):
    """A roster or its current file bytes do not match the required pins."""


def _path_syntax(path):
    value = Path(path)
    if (not value.is_absolute() or '..' in value.parts
            or str(value) != path):
        raise SourceIntegrityError('noncanonical_source_path')
    return value


def _canonical(path):
    value = _path_syntax(path)
    if value.resolve() != value:
        raise SourceIntegrityError('noncanonical_source_path')
    if any(item.is_symlink() for item in (value, *value.parents)):
        raise SourceIntegrityError('symlink_source_path')
    return value


def verify_source_phase(*rosters):
    """Read and verify the first-occurrence union of all supplied pin rosters.

    A roster is a list or tuple of exact ``path/bytes/sha256`` dictionaries.
    Empty component rosters are allowed; the complete union must be nonempty.
    Conflicting pins for one path and malformed inputs fail before file reads.
    The return value is a count, not a reusable proof for a later boundary.

    The union is local to this call. A caller must still invoke its independent
    before/after, request-preparation, and worker checks at their original
    boundaries. Changes after this call are detected by those later calls.
    """
    if not rosters:
        raise SourceIntegrityError('source_rosters_required')
    unique = {}
    for roster in rosters:
        if type(roster) not in (list, tuple):
            raise SourceIntegrityError('invalid_source_roster')
        for reference in roster:
            if (type(reference) is not dict
                    or set(reference) != {'path', 'bytes', 'sha256'}
                    or type(reference['path']) is not str
                    or '\x00' in reference['path']
                    or type(reference['bytes']) is not int
                    or reference['bytes'] < 0
                    or type(reference['sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}', reference['sha256']) is None):
                raise SourceIntegrityError('invalid_source_pin')
            frozen = dict(reference)
            path = frozen['path']
            _path_syntax(path)
            if path in unique and unique[path] != frozen:
                raise SourceIntegrityError('conflicting_source_pin:' + path)
            unique[path] = frozen
    if not unique:
        raise SourceIntegrityError('empty_source_union')
    verified_bytes = 0
    for reference in unique.values():
        path = _canonical(reference['path'])
        if not path.is_file():
            raise SourceIntegrityError('missing_regular_source:' + str(path))
        raw = path.read_bytes()
        actual = dict(path=str(path), bytes=len(raw),
                      sha256=hashlib.sha256(raw).hexdigest())
        if actual != reference:
            raise SourceIntegrityError('source_pin_mismatch:' + str(path))
        verified_bytes += len(raw)
    return dict(unique_references=len(unique), verified_bytes=verified_bytes)
