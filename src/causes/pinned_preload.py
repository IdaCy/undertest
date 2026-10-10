"""Single-use immutable preparation with complete file checks at consumption."""

import copy
import hashlib
import os
from pathlib import Path
import stat


def _identity(value):
    return tuple(getattr(value, key) for key in
                 ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _reference(value):
    if (type(value) is not dict or set(value) != {'path', 'bytes', 'sha256'}
            or type(value['path']) is not str or type(value['bytes']) is not int
            or value['bytes'] < 0 or type(value['sha256']) is not str
            or len(value['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['sha256'])):
        raise ValueError('invalid_preload_reference')
    return copy.deepcopy(value)


def _checked(reference, previous=None, retain=False):
    path = Path(reference['path'])
    if (not path.is_absolute() or str(path) != reference['path']
            or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents))):
        raise ValueError('noncanonical_preload_path')
    before = path.lstat()
    identity = _identity(before)
    if (not stat.S_ISREG(before.st_mode) or before.st_size != reference['bytes']
            or previous is not None and identity != previous):
        raise ValueError('preload_file_identity_changed')
    digest, size, chunks = hashlib.sha256(), 0, []
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != identity:
            raise ValueError('preload_file_replaced_before_read')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
            if size > reference['bytes']:
                raise ValueError('preload_file_grew')
            if retain:
                chunks.append(block)
        if _identity(os.fstat(stream.fileno())) != identity:
            raise ValueError('preload_file_changed_during_read')
    if (_identity(path.lstat()) != identity or path.resolve() != path
            or any(p.is_symlink() for p in (path, *path.parents))
            or size != reference['bytes'] or digest.hexdigest() != reference['sha256']):
        raise ValueError('preload_file_changed')
    return (b''.join(chunks) if retain else None), identity


class PinnedPreload:
    """Build once, transfer one result, and verify original bytes again at finish.

    The builder receives a pinned byte reader. It must perform only immutable
    preparation; callers keep clocks, current observations and authority checks
    outside this object. No computed result is serialized or reused by another
    invocation. Source and preparation pins can be included as guard references.
    """

    def __init__(self):
        self._state = 'new'
        self._references = {}
        self._identities = {}
        self._value = None

    @property
    def references(self):
        return tuple(copy.deepcopy(value) for value in self._references.values())

    def _read(self, value):
        if self._state != 'building':
            raise ValueError('preload_reader_outside_build')
        try:
            return self._recorded_read(value)
        except BaseException:
            self._state, self._value = 'invalid', None
            raise

    def _recorded_read(self, value):
        reference = _reference(value)
        path = reference['path']
        if path in self._references and self._references[path] != reference:
            raise ValueError('preload_conflicting_pin')
        raw, identity = _checked(reference, self._identities.get(path), retain=True)
        self._references[path] = reference
        self._identities[path] = identity
        return raw

    def _verify(self):
        for path, reference in self._references.items():
            _checked(reference, self._identities[path])

    def prepare(self, builder, *, guard_references=()):
        if self._state != 'new':
            raise ValueError('preload_preparation_not_new')
        self._state = 'building'
        try:
            for reference in guard_references:
                self._read(reference)
            self._value = builder(self._read)
            if self._state != 'building':
                raise ValueError('preload_builder_suppressed_failure')
            self._verify()
            self._state = 'prepared'
        except BaseException:
            self._state, self._value = 'invalid', None
            raise

    def take(self):
        if self._state != 'prepared':
            raise ValueError('preload_not_prepared')
        try:
            self._verify()
            value, self._value = self._value, None
            self._state = 'consumed'
            return value
        except BaseException:
            self._state, self._value = 'invalid', None
            raise

    def finish(self):
        if self._state != 'consumed':
            raise ValueError('preload_not_consumed')
        try:
            self._verify()
            self._state = 'finished'
        except BaseException:
            self._state = 'invalid'
            raise
