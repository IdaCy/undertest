"""Durable process and barrier primitives for a separately validated handoff.

Callers supply the scientific, custody, registration and resource policy checks.
These primitives do not establish that policy or authorize another attempt.
"""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import stat
import time


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('noncanonical_handoff_path')
    return path


def pin(path, raw=None):
    path = canonical_path(path)
    raw = path.read_bytes() if raw is None else raw
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def checked_bytes(reference):
    path = canonical_path(reference['path'])
    raw = path.read_bytes()
    if type(reference['bytes']) is not int or pin(path, raw) != reference:
        raise ValueError('handoff_input_pin_changed')
    return raw


def checked(reference):
    return json.loads(checked_bytes(reference))


def sync_directory(path):
    descriptor = os.open(canonical_path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_new(directory, name, value):
    if Path(name).name != name or name in ('', '.', '..'):
        raise ValueError('one_receipt_filename_required')
    path = canonical_path(directory) / name
    raw = encoded(value) + b'\n'
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)
    return pin(path, raw)


def new_record_directory(path):
    path = canonical_path(path)
    path.mkdir(exist_ok=False)
    sync_directory(path.parent)
    return path


@dataclass(frozen=True)
class Authority:
    reference: dict
    action: str
    pause_paths: tuple[Path, ...]
    validate_policy: Callable

    def check(self):
        value = checked(self.reference)
        if value.get('root_authorized') is not True or value.get('no_user_revocation') is not True or value.get('action') != self.action:
            raise ValueError('wrong_handoff_authority')
        started = dt.datetime.fromisoformat(value['issued_at'])
        expires = dt.datetime.fromisoformat(value['expires_at'])
        current = dt.datetime.now(dt.timezone.utc)
        if started.tzinfo is None or expires.tzinfo is None or not 0 < (expires - started).total_seconds() <= 180 or not started <= current < expires:
            raise ValueError('handoff_authority_not_fresh')
        if not self.action or type(self.pause_paths) is not tuple:
            raise ValueError('invalid_handoff_authority_scope')
        for path in self.pause_paths:
            if os.path.lexists(canonical_path(path)):
                raise ValueError('pause_blocks_handoff')
        if self.validate_policy(value) is not True:
            raise ValueError('handoff_policy_not_validated')
        # Policy checks may take time or observe a concurrent user pause.
        checked_bytes(self.reference)
        if not started <= dt.datetime.now(dt.timezone.utc) < expires:
            raise ValueError('handoff_authority_not_fresh')
        if any(os.path.lexists(canonical_path(path)) for path in self.pause_paths):
            raise ValueError('pause_blocks_handoff')
        return value


def process_identity(expected):
    pid = expected['pid']
    ticks = expected['process_starttime_ticks']
    argv = expected['argv']
    if type(pid) is not int or pid <= 0 or type(ticks) not in (int, str) or not str(ticks).isdigit() or int(ticks) <= 0:
        raise ValueError('invalid_process_identity')
    if not isinstance(argv, list) or not argv or any(not isinstance(value, str) for value in argv):
        raise ValueError('invalid_process_argv')
    directory = Path('/proc') / str(pid)
    before = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    actual = [value.decode() for value in (directory / 'cmdline').read_bytes().split(b'\0') if value]
    after = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    if before[19] != after[19] or after[19] != str(ticks) or after[0] in ('Z', 'X', 'x') or actual != argv:
        raise ValueError('process_identity_changed')
    return {'pid': pid, 'process_starttime_ticks': after[19], 'argv': actual}


def lock_owned_by(path, pid):
    value = canonical_path(path).stat()
    owners = []
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[1] == 'FLOCK' and fields[3] == 'WRITE':
            device = fields[5].split(':')
            if len(device) == 3 and int(device[0], 16) == os.major(value.st_dev) and int(device[1], 16) == os.minor(value.st_dev) and int(device[2]) == value.st_ino:
                owners.append(int(fields[4]))
    return owners == [pid]


def lock_free(path):
    with canonical_path(path).open('rb') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream, fcntl.LOCK_UN)
    return True


def retirement_state(expected):
    path = Path('/proc') / str(expected['pid']) / 'stat'
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {'pid': expected['pid'], 'absent': True}
    fields = raw.decode().rsplit(')', 1)[1].split()
    if fields[19] != str(expected['process_starttime_ticks']):
        raise ValueError('retired_pid_reused')
    return {'pid': expected['pid'], 'absent': False, 'state': fields[0],
            'start_ticks': fields[19], 'stat_sha256': hashlib.sha256(raw).hexdigest()}


def retire_exact_idle_owner(process, output_root, journal, record_directory, authority: Authority):
    root = canonical_path(output_root)
    lock = root / '.dispatcher.lock'
    if journal['path'] != str(root / '.dispatcher/journal.jsonl'):
        raise ValueError('retirement_journal_scope_changed')

    def validate_idle():
        authority.check()
        checked_bytes(journal)
        identity = process_identity(process)
        children = (Path('/proc') / str(process['pid']) / 'task' / str(process['pid']) / 'children').read_text().split()
        if children or not lock_owned_by(lock, process['pid']):
            raise ValueError('owner_not_exact_idle_lock_holder')
        return identity

    identity = validate_idle()
    directory = new_record_directory(record_directory)
    signaled = False
    observations = []
    try:
        intent = save_new(directory, 'INTENT.json', {'at': now(), 'authority': authority.reference,
                          'process': identity, 'output_root': str(root), 'journal': journal,
                          'signal': 'SIGTERM', 'one_signal': True})
        validate_idle()
        lock_identity = (lock.stat().st_dev, lock.stat().st_ino)
        os.kill(process['pid'], signal.SIGTERM)
        signaled = True
        deadline = time.monotonic() + 15
        previous_dead = None
        while time.monotonic() < deadline:
            state = retirement_state(process)
            current_lock = canonical_path(lock).stat()
            if (current_lock.st_dev, current_lock.st_ino) != lock_identity:
                raise ValueError('retirement_lock_changed')
            free = lock_free(lock)
            observations.append({**state, 'at': now(), 'output_lock_free': free})
            dead = state.get('state') in ('Z', 'X', 'x')
            stable_dead = dead and previous_dead == (state.get('state'), state.get('start_ticks'))
            if free and (state['absent'] or stable_dead):
                return save_new(directory, 'RETIREMENT.json', {'at': now(), 'intent': intent,
                                'process': identity, 'output_root': str(root), 'observations': observations,
                                'retired': True, 'output_lock_free': True})
            previous_dead = (state.get('state'), state.get('start_ticks')) if dead else None
            time.sleep(.1)
        raise ValueError('retirement_unresolved_no_retry')
    except BaseException as error:
        save_new(directory, 'FAILED.json', {'at': now(), 'error_type': type(error).__name__,
                 'signal_sent': signaled, 'observations': observations, 'manual_reconciliation_required': True})
        raise


def stat_identity(path):
    value = canonical_path(path).lstat()
    return {key: getattr(value, key) for key in
            ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')}


def barrier_path(path):
    path = canonical_path(path)
    if path.name in ('PAUSE', 'USER_PAUSE', 'GO_REVOKED'):
        raise ValueError('pause_marker_is_not_an_owned_barrier')
    return path


def verify_owned_barrier(reference, path):
    path = barrier_path(path)
    owned = checked(reference)
    if owned['marker']['path'] != str(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('owned_barrier_path_or_type_changed')
    raw = checked_bytes(owned['marker'])
    if encoded(stat_identity(path)) != encoded(owned['stat']) or checked_bytes(owned['archived_marker']) != raw:
        raise ValueError('owned_barrier_identity_changed')
    return owned


def create_owned_barrier(path, output_root, label, record_directory, authority: Authority):
    path = barrier_path(path)
    output = canonical_path(output_root)
    if not isinstance(label, str) or not label or os.path.lexists(path):
        raise ValueError('new_owned_barrier_required')
    authority.check()
    directory = new_record_directory(record_directory)
    created = False
    try:
        intent = save_new(directory, 'INTENT.json', {'at': now(), 'authority': authority.reference,
                          'barrier_path': str(path), 'output_root': str(output), 'label': label})
        raw = encoded({'schema': 'causes.owned_start_barrier.v1', 'at': now(), 'authority': authority.reference,
                       'output_root': str(output), 'label': label}) + b'\n'
        authority.check()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(path.parent)
        archive = directory / 'BARRIER.created'
        with archive.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        owned = {'creation_intent': intent, 'creation_authority': authority.reference, 'marker': pin(path, raw),
                 'stat': stat_identity(path), 'archived_marker': pin(archive, raw), 'output_root': str(output), 'label': label}
        reference = save_new(directory, 'OWNERSHIP.json', owned)
        verify_owned_barrier(reference, path)
        return reference
    except BaseException as error:
        save_new(directory, 'FAILED.json', {'at': now(), 'error_type': type(error).__name__,
                 'barrier_created': created, 'manual_reconciliation_required': True})
        raise


def release_owned_barrier(reference, path, record_directory, authority: Authority, validate_waiting: Callable):
    path = barrier_path(path)

    def validate():
        authority.check()
        owned = verify_owned_barrier(reference, path)
        if validate_waiting(owned) is not True:
            raise ValueError('waiting_owner_not_validated')
        authority.check()
        verify_owned_barrier(reference, path)
        return owned

    owned = validate()
    directory = new_record_directory(record_directory)
    removed = False
    try:
        intent = save_new(directory, 'INTENT.json', {'at': now(), 'authority': authority.reference,
                          'ownership': reference, 'owned_barrier': owned, 'no_pause_marker_mutation': True})
        validate()
        path.unlink()
        removed = True
        sync_directory(path.parent)
        if os.path.lexists(path):
            raise ValueError('owned_barrier_reappeared')
        return save_new(directory, 'RESULT.json', {'at': now(), 'intent': intent, 'released_owned_barrier': reference,
                        'barrier_path': str(path), 'no_pause_marker_removed': True})
    except BaseException as error:
        save_new(directory, 'FAILED.json', {'at': now(), 'error_type': type(error).__name__,
                 'barrier_removed': removed, 'manual_reconciliation_required': True})
        raise


def record_and_exec(receipt, argv, intent):
    receipt = canonical_path(receipt)
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or '\x00' in item for item in argv) or not Path(argv[0]).is_absolute():
        raise ValueError('exact_absolute_exec_argv_required')
    value = checked(intent)
    if value.get('argv') != argv:
        raise ValueError('exec_intent_argv_changed')
    fields = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    save_new(receipt.parent, receipt.name, {'at': now(), 'pid': os.getpid(),
             'process_starttime_ticks': int(fields[19]), 'argv': argv,
             'intent_path': intent['path'], 'intent_sha256': intent['sha256']})
    os.execvp(argv[0], argv)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('receipt', type=Path)
    parser.add_argument('argv_json')
    parser.add_argument('intent', type=Path)
    parser.add_argument('intent_sha256')
    args = parser.parse_args()
    reference = pin(args.intent)
    if reference['sha256'] != args.intent_sha256:
        raise ValueError('exec_intent_pin_changed')
    record_and_exec(args.receipt, json.loads(args.argv_json), reference)


if __name__ == '__main__':
    main()
