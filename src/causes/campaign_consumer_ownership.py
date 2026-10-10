"""Exclusive local workspaces for consumers that coexist with another observer.

This module grants no runtime authority and starts no consumers. The caller must
bind and authorize the exact configuration, protect existing inputs, and keep
the workspace context open through consumer execution and readiness assembly.
No historical observer STOP is accepted as proof of current ownership.
"""
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    path = Path(value)
    require(path.is_absolute() and str(path) == str(value) and path.resolve() == path
            and not any(p.is_symlink() for p in (path, *path.parents)), "noncanonical_workspace_path")
    return path


def pin(value):
    path = canonical(value)
    raw = path.read_bytes()
    return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def checked(ref):
    require(pin(ref['path']) == ref, "workspace_input_changed")
    return json.loads(Path(ref['path']).read_bytes())


def mapped(reference_map, remote):
    """Read one exact retained alias without importing a remote runtime package."""
    matches = [row for row in checked(reference_map)['files'] if row['remote'] == remote]
    require(len(matches) == 1, 'unique_original_receipt_required')
    row = matches[0]
    require(pin(row['local']['path']) == row['local'], 'retained_receipt_changed')
    raw = Path(row['local']['path']).read_bytes()
    require(len(raw) == remote['bytes'] and hashlib.sha256(raw).hexdigest() == remote['sha256'],
            'mapped_receipt_changed')
    return row, raw


def save(path, value):
    path = canonical(path)
    with path.open('xb') as stream:
        stream.write((json.dumps(value, indent=2, sort_keys=True) + '\n').encode())
        stream.flush(); os.fsync(stream.fileno())
    with directory_fd(path.parent) as fd:
        os.fsync(fd)
    return pin(path)


@contextlib.contextmanager
def directory_fd(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def process(pid):
    path = Path('/proc') / str(pid)
    state = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    require(state[0] not in ('Z', 'X', 'x'), "workspace_owner_not_live")
    return dict(pid=pid, process_starttime_ticks=state[19],
                argv=[x.decode() for x in (path / 'cmdline').read_bytes().split(b'\0') if x])


def lock_stat(path):
    st = canonical(path).stat()
    return dict(st_dev=st.st_dev, st_ino=st.st_ino)


def lock_owners(path):
    st = canonical(path).stat(); owners = []
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[1] == 'FLOCK' and fields[3] == 'WRITE':
            dev = fields[5].split(':')
            if (len(dev) == 3 and int(dev[0], 16) == os.major(st.st_dev)
                    and int(dev[1], 16) == os.minor(st.st_dev) and int(dev[2]) == st.st_ino):
                owners.append(int(fields[4]))
    return owners


def layout(root):
    root = canonical(root)
    return dict(workspace=str(root), status_directory=str(root / 'status'),
                transport_directory=str(root / 'transport'), snapshot_destination=str(root / 'snapshots'),
                execution_directory=str(root / 'execution'), ownership_lock=str(root / 'owner.lock'))


def validate_layout(value, protected_directories):
    require(value == layout(value['workspace']), "workspace_layout_changed")
    root = canonical(value['workspace'])
    require(type(protected_directories) is list and protected_directories, "protected_directories_required")
    for other in protected_directories:
        other = canonical(other)
        require(not (root == other or root.is_relative_to(other) or other.is_relative_to(root)),
                "workspace_overlaps_existing_consumer")
    return root


def isolate_config(config, root, protected_directories):
    """Change exactly three local output directories; retain all scientific inputs."""
    value = layout(root); validate_layout(value, protected_directories)
    require(config['snapshot']['destination'] in protected_directories
            and config['status_directory'] in protected_directories
            and config['transport_directory'] in protected_directories, "original_outputs_not_protected")
    result = copy.deepcopy(config)
    result['snapshot']['destination'] = value['snapshot_destination']
    result['status_directory'] = value['status_directory']
    result['transport_directory'] = value['transport_directory']
    return result


def configuration_layout(config, expected):
    require(config['snapshot']['destination'] == expected['snapshot_destination']
            and config['status_directory'] == expected['status_directory']
            and config['transport_directory'] == expected['transport_directory'], "consumer_output_layout_changed")


def protected_state(pins, processes):
    require(type(pins) is list and pins and len({x['path'] for x in pins}) == len(pins),
            "protected_pins_invalid")
    for ref in pins:
        require(pin(ref['path']) == ref, "protected_input_changed")
    require(type(processes) is list and len({x['pid'] for x in processes}) == len(processes),
            "protected_processes_invalid")
    for item in processes:
        require(process(item['pid']) == item, "protected_process_changed")
    return dict(pins=copy.deepcopy(pins), processes=copy.deepcopy(processes))


class Workspace:
    """A fresh namespace plus a live, exclusive owner held through assembly.

    The caller owns authorization. An existing workspace is always refused,
    including one left by failure. The native snapshot keeps its separate
    inner sync.lock; this outer lock is never a substitute for that lock.
    """
    def __init__(self, contract, plan_ref, adoption_ref):
        self.contract = copy.deepcopy(contract)
        self.plan_ref, self.adoption_ref = plan_ref, adoption_ref
        self.lock = None

    def __enter__(self):
        c = self.contract; root = validate_layout(c['layout'], c['protected_directories'])
        require(root.parent.is_dir(), "workspace_parent_missing")
        before = protected_state(c['protected_pins'], c['protected_processes'])
        root.mkdir(mode=0o700)
        with directory_fd(root.parent) as fd: os.fsync(fd)
        try:
            self.lock = (root / 'owner.lock').open('xb')
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in ('status', 'transport', 'execution'):
                (root / name).mkdir(mode=0o700)
            with directory_fd(root) as fd: os.fsync(fd)
            self.begin = dict(schema='causes.isolated_consumer_ownership_begin.v1',
                at=dt.datetime.now(dt.timezone.utc).isoformat(), plan=self.plan_ref,
                adoption=self.adoption_ref, contract=c, owner=process(os.getpid()),
                lock_stat=lock_stat(root / 'owner.lock'), protected_before=before,
                workspace_created_exclusively=True, snapshot_destination_initially_absent=True,
                historical_stop_used=False)
            self.begin_ref = save(root / 'OWNERSHIP_BEGIN.json', self.begin)
            return self
        except BaseException:
            if self.lock: self.lock.close()
            raise

    def seal(self, receipts):
        """Record successful child receipts while ownership is still live."""
        require(self.lock is not None and not self.lock.closed, "workspace_not_owned")
        require(set(receipts) == {'status', 'sync'}, "consumer_pair_required")
        for ref in receipts.values():
            value = checked(ref)
            require(value['plan'] == self.plan_ref and value['returncode'] == 0
                    and value['error'] is None, "consumer_execution_failed")
        after = protected_state(self.contract['protected_pins'], self.contract['protected_processes'])
        require(after == self.begin['protected_before'], "protected_boundary_changed")
        root = Path(self.contract['layout']['workspace'])
        require(process(os.getpid()) == self.begin['owner']
                and lock_owners(root / 'owner.lock') == [os.getpid()]
                and lock_stat(root / 'owner.lock') == self.begin['lock_stat'], "workspace_owner_changed")
        self.actual = save(root / 'OWNERSHIP_ACTUAL.json', dict(
            schema='causes.isolated_consumer_ownership_actual.v1',
            at=dt.datetime.now(dt.timezone.utc).isoformat(), begin=self.begin_ref,
            plan=self.plan_ref, adoption=self.adoption_ref, receipts=receipts,
            protected_after=after, owner=self.begin['owner'], lock_stat=self.begin['lock_stat'],
            live_exclusive_owner_verified=True, historical_stop_used=False))
        return self.actual

    def __exit__(self, kind, value, traceback):
        boundary_error = None
        try:
            root = Path(self.contract['layout']['workspace'])
            after = None
            try:
                after = protected_state(self.contract['protected_pins'], self.contract['protected_processes'])
            except BaseException as exc:
                boundary_error = dict(type=type(exc).__name__, message=str(exc))
            save(root / 'OWNERSHIP_END.json', dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),
                begin=self.begin_ref, actual=getattr(self, 'actual', None),
                failure=None if kind is None else dict(type=kind.__name__, message=str(value)),
                protected_after=after, boundary_observation_error=boundary_error,
                owner=process(os.getpid()), lock_stat=lock_stat(root / 'owner.lock'),
                lock_owned_before_release=lock_owners(root / 'owner.lock') == [os.getpid()]))
        finally:
            self.lock.close()
        if kind is None and boundary_error is not None:
            raise ValueError('protected_boundary_observation_failed:' + boundary_error['message'])


def verify_live_ownership(ref, plan_ref, contract, receipts):
    actual = checked(ref); begin = checked(actual['begin'])
    root = validate_layout(contract['layout'], contract['protected_directories'])
    require(ref['path'] == str(root / 'OWNERSHIP_ACTUAL.json')
            and actual['begin']['path'] == str(root / 'OWNERSHIP_BEGIN.json'), "ownership_receipt_path_changed")
    require(actual['schema'] == 'causes.isolated_consumer_ownership_actual.v1'
            and begin['schema'] == 'causes.isolated_consumer_ownership_begin.v1', "ownership_schema_changed")
    require(actual['plan'] == begin['plan'] == plan_ref and actual['adoption'] == begin['adoption']
            and begin['contract'] == contract and actual['receipts'] == receipts, "ownership_binding_changed")
    require(begin['workspace_created_exclusively'] is True and begin['snapshot_destination_initially_absent'] is True
            and actual['live_exclusive_owner_verified'] is True
            and actual['historical_stop_used'] is False and begin['historical_stop_used'] is False,
            "isolated_ownership_required")
    require(actual['owner'] == begin['owner'] == process(actual['owner']['pid'])
            and actual['lock_stat'] == begin['lock_stat'] == lock_stat(root / 'owner.lock')
            and lock_owners(root / 'owner.lock') == [actual['owner']['pid']], "workspace_not_currently_owned")
    require(actual['protected_after'] == begin['protected_before']
            == protected_state(contract['protected_pins'], contract['protected_processes']),
            "protected_boundary_changed")
    return actual


def verify_snapshot_storage(sync, manifest, verified, value):
    """Verify physical custody and the entire fresh isolated accepted index.

    Call the unchanged consumer_join.verify_snapshot_result first to validate
    native scope, totals, authorization and one-to-one manifest/receipt joins.
    """
    destination = canonical(value['snapshot_destination']); attempt = canonical(sync['attempt'])
    require(attempt.parent == destination / 'attempts', "snapshot_outside_isolated_destination")
    rows = verified['runs']
    raw = (destination / 'accepted.jsonl').read_bytes()
    require(not raw or raw.endswith(b'\n'), "unterminated_isolated_index")
    require([json.loads(line) for line in raw.splitlines()] == rows, "isolated_index_not_exact_new_receipts")
    for row in manifest['runs']:
        for item in row['files']:
            expected = dict(path=str(attempt / 'data' / row['run'] / item['path']),
                            bytes=item['bytes'], sha256=item['sha256'])
            require(pin(expected['path']) == expected, "snapshot_custody_changed")
    with (destination / 'sync.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return dict(index=pin(destination / 'accepted.jsonl'), runs=len(rows),
                files=sum(len(row['files']) for row in manifest['runs']))
