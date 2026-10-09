"""Stage exact immutable files without replacing existing evidence."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('noncanonical_path')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('symlink_path')
    return path


def _pin(path, raw):
    return {'path': str(path), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def _read_exact(path, expected):
    canonical(path)
    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ValueError('existing_file_size_or_type')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected['bytes']:
            raise ValueError('existing_file_size_or_type')
        raw = stream.read(expected['bytes'] + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError('file_changed_during_read')
    if _pin(path, raw) != expected:
        raise ValueError('existing_file_pin_changed')
    return raw


def stage(packet, base, protected_paths=(), *, max_files=10000, max_bytes=1024 ** 3):
    """Preflight every input, then exclusively create missing immutable files.

    Existing files must match exactly. A partial write failure leaves evidence
    in place for caller reconciliation; this function never rolls back or retries.
    Protected paths may be verified but are never created or replaced.
    """
    base = canonical(base)
    if not base.is_dir() or type(max_files) is not int or max_files < 1:
        raise ValueError('invalid_base_or_file_limit')
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError('invalid_byte_limit')
    protected = tuple(canonical(p) for p in protected_paths)
    if any(not p.is_relative_to(base) for p in protected):
        raise ValueError('protected_path_outside_base')
    if not isinstance(packet, dict) or set(packet) != {'files', 'verify_only'}:
        raise ValueError('invalid_packet_shape')
    files, verify = packet['files'], packet['verify_only']
    if not isinstance(files, list) or not isinstance(verify, list) or len(files) + len(verify) > max_files:
        raise ValueError('invalid_file_inventory')
    ready = []
    seen = set()
    total = 0
    for rows, create in ((files, True), (verify, False)):
        for row in rows:
            keys = {'destination', 'bytes', 'sha256', 'base64'} if create else {'path', 'bytes', 'sha256'}
            if not isinstance(row, dict) or set(row) != keys:
                raise ValueError('invalid_file_shape')
            path = canonical(row['destination'] if create else row['path'])
            if path == base or not path.is_relative_to(base) or path in seen:
                raise ValueError('outside_base_or_duplicate_path')
            seen.add(path)
            for parent in path.parents:
                if os.path.lexists(parent):
                    if not parent.is_dir():
                        raise ValueError('existing_parent_is_not_directory')
                    break
            if type(row['bytes']) is not int or row['bytes'] < 0:
                raise ValueError('invalid_byte_count')
            total += row['bytes']
            if total > max_bytes:
                raise ValueError('byte_limit_exceeded')
            expected = {'path': str(path), 'bytes': row['bytes'], 'sha256': row['sha256']}
            if create:
                if any(path == p or path.is_relative_to(p) for p in protected):
                    raise ValueError('protected_path_must_be_verify_only')
                if not isinstance(row['base64'], str) or len(row['base64']) > ((row['bytes'] + 2) // 3) * 4:
                    raise ValueError('invalid_encoded_size')
                raw = base64.b64decode(row['base64'], validate=True)
                if _pin(path, raw) != expected:
                    raise ValueError('packet_pin_changed')
                if os.path.lexists(path):
                    _read_exact(path, expected)
                ready.append((path, raw, expected))
            else:
                _read_exact(path, expected)
    if any(parent in seen for path in seen for parent in path.parents):
        raise ValueError('overlapping_file_paths')
    receipts = []
    for path, raw, expected in ready:
        canonical(path)
        existed = os.path.lexists(path)
        if not existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            canonical(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _read_exact(path, expected)
        receipts.append({**expected, 'existing_identical': existed})
    return {'files': receipts, 'verify_only_count': len(verify), 'model_or_service_actions': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--protected-path', type=Path, action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(stage(json.load(sys.stdin), args.base, args.protected_path)))


if __name__ == '__main__':
    main()
