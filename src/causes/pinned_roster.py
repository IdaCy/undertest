"""Opt-in, action-local proof reuse for an explicitly immutable file roster.

This stores identities and digests, never file bodies or decoded values. A hit
checks the complete original identity through nofollow directory descriptors.
It does not hash the body again. A byte change that leaves the observed identity
unchanged can escape a hit; finish() hashes every body again and detects a
lasting change. This is a trusted-filesystem optimization, not continuous byte
immutability or a replacement for live guards, authority or job-specific reads.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import stat
import threading
import time


class RosterError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_files: int = 1024
    max_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 128 * 1024 * 1024
    max_metadata_bytes: int = 1024 * 1024
    max_parents: int = 4096
    max_path_bytes: int = 4096
    max_components: int = 64
    max_reuses: int = 32768
    phase_seconds: float = 60.0
    lock_seconds: float = 10.0

    def __post_init__(self):
        for key, value in vars(self).items():
            if key.endswith('_seconds'):
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise RosterError('invalid_roster_limit')
            elif type(value) is not int or value <= 0:
                raise RosterError('invalid_roster_limit')


@dataclass(frozen=True)
class _Pin:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Check:
    paths: int
    bytes: int
    roster_sha256: str
    body_bytes_read: int
    read_calls: int
    identity_checks: int
    reused: bool


def _file_identity(value):
    return tuple(getattr(value, key) for key in (
        'st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
        'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _parent_identity(value):
    # Directory timestamps, size and link count can change when an unrelated
    # sibling is published. Path custody still binds inode, type, owner and mode.
    return tuple(getattr(value, key) for key in (
        'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid'))


def _pins(values, limits):
    if type(values) not in (list, tuple) or not 0 < len(values) <= limits.max_files:
        raise RosterError('roster_count')
    result, seen, total, metadata = [], {}, 0, 0
    for value in values:
        if (type(value) is not dict or set(value) != {'path', 'bytes', 'sha256'}
                or type(value['path']) is not str or type(value['bytes']) is not int
                or not 0 <= value['bytes'] <= limits.max_file_bytes
                or type(value['sha256']) is not str or len(value['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in value['sha256'])):
            raise RosterError('roster_pin_schema')
        path = value['path']
        if len(os.fsencode(path)) > limits.max_path_bytes:
            raise RosterError('roster_noncanonical_path')
        parts = PurePosixPath(path).parts
        if (not path.startswith('/') or path.startswith('//') or '\x00' in path
                or str(PurePosixPath(path)) != path or '..' in parts or len(parts) < 2
                or len(parts) > limits.max_components):
            raise RosterError('roster_noncanonical_path')
        pin = _Pin(path, value['bytes'], value['sha256'])
        if path in seen:
            # Even identical duplicates are refused: the reviewed allowlist is
            # one explicit ordered entry per path, not a multiset to normalize.
            raise RosterError('roster_duplicate_path')
        seen[path] = pin
        total += pin.size
        metadata += len(json.dumps(value, sort_keys=True).encode())
        if total > limits.max_bytes or metadata > limits.max_metadata_bytes:
            raise RosterError('roster_budget')
        result.append(pin)
    return tuple(result), total, metadata


class _Budget:
    def __init__(self, proof, full):
        self.proof = proof
        self.deadline = time.monotonic() + proof._limits.phase_seconds
        self.requested = self.read_calls = self.body_bytes = self.identities = 0
        self.max_requested = proof._bytes + len(proof._pins) if full else 0
        self.max_calls = sum((p.size + 65535) // 65536 + 1 for p in proof._pins) if full else 0

    def check(self):
        if self.proof._poison.is_set():
            raise RosterError('roster_poisoned')
        if time.monotonic() >= self.deadline:
            raise RosterError('roster_deadline')

    def read(self, fd, size):
        self.check()
        self.requested += size
        self.read_calls += 1
        if self.requested > self.max_requested or self.read_calls > self.max_calls:
            raise RosterError('roster_read_budget')
        data = os.read(fd, size)
        self.body_bytes += len(data)
        self.check()
        return data


class ImmutableRosterProof:
    """Validate once, check exact identities on reuse, then fully revalidate.

    Callers must opt in and supply a reviewed, ordered, duplicate-free allowlist
    of immutable inputs. Keep current code, authority, owner and job-specific
    references outside it and fully validate those at their original boundaries.
    No allowlist refresh, changed pin, changed identity or recovery is supported.
    Any failed operation permanently poisons this instance, including a failed
    concurrent caller. All operations serialize and have bounded lock waits.

    Limits bound roster/parent metadata, streaming buffers, read requests, reuse
    count and cooperative phase time. They are not a total RSS or hard syscall
    deadline; the action's outer resource/time limits remain required. Parent
    metadata has a separate serialized accounting bound equal to the pin bound.
    File descriptors are bounded by path depth, and are never retained on hits.
    finish() must precede authoritative output/closure publication, including
    failure cleanup. It permanently closes the proof; it cannot retime a READY
    record or authorize a claim. The caller must join all source-check users
    before finish/publication. The proof is scoped to its creating process.
    """

    def __init__(self, allowlist, *, enabled=False, limits=None):
        if enabled is not True:
            raise RosterError('roster_reuse_requires_opt_in')
        self._limits = Limits() if limits is None else limits
        if type(self._limits) is not Limits:
            raise RosterError('invalid_roster_limits')
        self._pins, self._bytes, _ = _pins(allowlist, self._limits)
        serialized = json.dumps([dict(path=p.path, bytes=p.size, sha256=p.sha256)
                                 for p in self._pins], sort_keys=True, separators=(',', ':')).encode()
        self._roster_sha256 = hashlib.sha256(serialized).hexdigest()
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._poison = threading.Event()
        self._state = 'new'
        self._identities = {}
        self._parents = {}
        self._parent_bytes = 0
        self._reuses = 0

    @property
    def poisoned(self):
        return self._poison.is_set()

    @property
    def roster_sha256(self):
        return self._roster_sha256

    def invalidate(self):
        """Permanently refuse reuse after a caller's related validation fails."""
        self._poison.set()

    def _parent(self, path, identity, initial):
        if path not in self._parents:
            if not initial:
                raise RosterError('roster_parent_unregistered')
            cost = len(json.dumps([path, identity]).encode())
            if (len(self._parents) >= self._limits.max_parents
                    or self._parent_bytes + cost > self._limits.max_metadata_bytes):
                raise RosterError('roster_parent_budget')
            self._parents[path] = identity
            self._parent_bytes += cost
        if self._parents[path] != identity:
            raise RosterError('roster_parent_changed')

    @contextmanager
    def _opened(self, pin, budget, initial):
        descriptors, links = [], []
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            budget.check()
            root = os.open('/', directory_flags)
            descriptors.append(root)
            root_identity = _parent_identity(os.fstat(root))
            self._parent('/', root_identity, initial)
            parent, prefix = root, ''
            components = PurePosixPath(pin.path).parts[1:]
            for name in components[:-1]:
                budget.check()
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise RosterError('roster_parent_not_directory')
                identity = _parent_identity(before)
                child = os.open(name, directory_flags, dir_fd=parent)
                descriptors.append(child)
                if _parent_identity(os.fstat(child)) != identity:
                    raise RosterError('roster_parent_open_race')
                prefix += '/' + name
                self._parent(prefix, identity, initial)
                links.append((parent, name, child, identity))
                parent = child
            name = components[-1]
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            identity = _file_identity(before)
            if not stat.S_ISREG(before.st_mode) or before.st_size != pin.size:
                raise RosterError('roster_file_type_or_size')
            if not initial and self._identities[pin.path] != identity:
                raise RosterError('roster_file_identity_changed')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=parent)
            descriptors.append(fd)
            if _file_identity(os.fstat(fd)) != identity:
                raise RosterError('roster_file_open_race')
            budget.identities += 1
            yield fd, identity
            budget.check()
            if (_file_identity(os.fstat(fd)) != identity
                    or _file_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity):
                raise RosterError('roster_file_changed_during_check')
            for parent, name, child, expected in reversed(links):
                budget.check()
                if (_parent_identity(os.fstat(child)) != expected
                        or _parent_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected):
                    raise RosterError('roster_parent_changed_during_check')
            if (_parent_identity(os.fstat(root)) != root_identity
                    or _parent_identity(os.stat('/', follow_symlinks=False)) != root_identity):
                raise RosterError('roster_root_changed')
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def _scan(self, budget, *, full, initial=False):
        for pin in self._pins:
            with self._opened(pin, budget, initial) as (fd, identity):
                if full:
                    digest, remaining = hashlib.sha256(), pin.size
                    while remaining:
                        requested = min(65536, remaining)
                        data = budget.read(fd, requested)
                        if len(data) != requested:
                            raise RosterError('roster_short_read')
                        digest.update(data)
                        remaining -= len(data)
                    if budget.read(fd, 1) or digest.hexdigest() != pin.sha256:
                        raise RosterError('roster_content_changed')
            if initial:
                self._identities[pin.path] = identity

    def _operation(self, kind, roster=None):
        acquired = False
        try:
            if os.getpid() != self._pid:
                raise RosterError('roster_process_changed')
            if self._poison.is_set():
                raise RosterError('roster_poisoned')
            if kind != 'validate' and _pins(roster, self._limits)[0] != self._pins:
                raise RosterError('roster_allowlist_changed')
            acquired = self._lock.acquire(timeout=self._limits.lock_seconds)
            if not acquired:
                raise RosterError('roster_lock_timeout')
            required = 'new' if kind == 'validate' else 'validated'
            if self._state != required:
                raise RosterError('roster_invalid_state')
            if kind == 'verify' and self._reuses >= self._limits.max_reuses:
                raise RosterError('roster_reuse_budget')
            budget = _Budget(self, full=kind != 'verify')
            budget.check()
            self._scan(budget, full=kind != 'verify', initial=kind == 'validate')
            # Revisit earlier files after the full sweep. Never refresh an
            # identity when a file changed while a later file was being read.
            self._scan(budget, full=False)
            budget.check()
            if kind == 'verify':
                self._reuses += 1
            self._state = 'finished' if kind == 'finish' else 'validated'
            return Check(len(self._pins), self._bytes, self.roster_sha256,
                         budget.body_bytes, budget.read_calls, budget.identities, kind == 'verify')
        except BaseException:
            self._poison.set()
            raise
        finally:
            if acquired:
                self._lock.release()

    def validate(self):
        """Perform the one initial, uncached full validation."""
        return self._operation('validate')

    def verify(self, allowlist):
        """Require the identical allowlist and original observable identities."""
        return self._operation('verify', allowlist)

    def finish(self, allowlist):
        """Uncached full hashes and original identities; close before publishing."""
        return self._operation('finish', allowlist)
