"""Continuously copy a fixed selection with native snapshot admission per pass."""

from dataclasses import asdict
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import time

from causes import campaign_snapshot as snapshot


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write(path, value):
    # Native save is exclusive and fsyncs the file; retain each cycle permanently.
    snapshot.save(path, value)
    snapshot.fsync_directory(path.parent)


def _index(config, previous):
    destination = snapshot.canonical_path(config.destination)
    with snapshot.canonical_path(destination / 'sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        path = snapshot.canonical_path(destination / config.index_name)
        raw = path.read_bytes() if path.exists() else b''
    if not raw.startswith(previous) or (raw and not raw.endswith(b'\n')):
        raise ValueError('snapshot_watch_index_prefix_changed_or_torn')
    runs = [json.loads(line)['run'] for line in raw.splitlines() if line]
    if len(set(runs)) != len(runs) or any(
            str(snapshot.relative_path(run).parent) not in config.roots for run in runs):
        raise ValueError('snapshot_watch_index_scope_or_duplicates')
    return raw, set(runs)


def _wait(stop, seconds):
    deadline = time.monotonic() + seconds
    while not stop.exists() and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def watch_completed(config, authorize, directory, *, interval_seconds=60):
    """Run until all selected runs are accepted, STOP exists, or a failure occurs.

    A full batch starts another native pass promptly. Otherwise wait for the
    configured interval. A native space hold polls only local free space until
    the retained requirement fits, then requests a fresh guarded manifest.
    All other errors latch FAILED.json and exit, with no automatic restart.
    Use a new reviewed directory after reconciling any failed invocation.
    """
    config.validate()
    if config.selected_runs is None:
        raise ValueError('snapshot_watch_requires_finite_selection')
    if type(interval_seconds) is not int or interval_seconds <= 0:
        raise ValueError('invalid_snapshot_watch_interval')
    directory = snapshot.canonical_path(directory)
    directory.mkdir(exist_ok=False)
    stop = directory / 'STOP'
    cycle, previous, hold = 0, b'', None
    with (directory / 'watch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            sources = {str(Path(module).resolve()): snapshot.digest(Path(module))
                       for module in (__file__, snapshot.__file__)}
            authorization = snapshot.checked_authorization(config, authorize)
            authorization_json = json.dumps(authorization, sort_keys=True, allow_nan=False)

            def stable_authorize(actual):
                if actual != config:
                    raise ValueError('snapshot_watch_configuration_changed')
                value = snapshot.checked_authorization(actual, authorize)
                if json.dumps(value, sort_keys=True, allow_nan=False) != authorization_json:
                    raise ValueError('snapshot_watch_authorization_changed')
                return value

            Path(config.destination).mkdir(parents=True, exist_ok=True)
            settings = asdict(config)
            settings['destination'] = str(settings['destination'])
            _write(directory / 'START.json', {
                'at': _now(), 'configuration': settings, 'authorization': authorization,
                'interval_seconds': interval_seconds, 'sources': sources,
                'stop': str(stop), 'automatic_retry': False})
            while True:
                if stop.exists():
                    result = {'at': _now(), 'reason': 'owned_stop', 'cycles': cycle}
                    _write(directory / 'STOPPED.json', result)
                    return result
                for path, digest in sources.items():
                    if snapshot.digest(Path(path)) != digest:
                        raise ValueError('snapshot_watch_source_changed')
                stable_authorize(config)
                previous, accepted = _index(config, previous)
                if set(config.selected_runs) <= accepted:
                    result = {'at': _now(), 'reason': 'selection_complete', 'cycles': cycle,
                              'selected_runs': len(config.selected_runs),
                              'index_bytes': len(previous),
                              'index_sha256': hashlib.sha256(previous).hexdigest()}
                    _write(directory / 'COMPLETE.json', result)
                    return result
                folder = directory / f'{cycle:08d}'
                folder.mkdir()
                _write(folder / 'INTENT.json', {'at': _now(), 'cycle': cycle,
                       'prior_index_bytes': len(previous),
                       'prior_index_sha256': hashlib.sha256(previous).hexdigest(),
                       'retained_space_hold': hold})
                full_batch = False
                available = shutil.disk_usage(config.destination).free if hold is not None else None
                if hold is not None and available < (
                        hold['required_bytes'] + config.free_space_floor_bytes):
                    outcome = {'kind': 'storage_held', 'hold': hold, 'remote_call': False,
                               'available_bytes_now': available}
                else:
                    hold = None
                    try:
                        value = snapshot.synchronize_completed(config, stable_authorize)
                        if not 0 <= value['verified_runs'] <= config.limit:
                            raise ValueError('snapshot_watch_native_batch_limit_changed')
                        full_batch = value['verified_runs'] == config.limit
                        outcome = {'kind': 'synchronized', 'native_result': value}
                    except snapshot.SnapshotSpaceHold as error:
                        if error.floor_bytes != config.free_space_floor_bytes:
                            raise ValueError('snapshot_watch_native_floor_changed')
                        hold = {'attempt': str(error.attempt), 'required_bytes': error.required_bytes,
                                'available_bytes': error.available_bytes,
                                'floor_bytes': error.floor_bytes}
                        outcome = {'kind': 'storage_held', 'hold': hold, 'remote_call': True}
                previous, accepted = _index(config, previous)
                _write(folder / 'RESULT.json', dict(outcome, at=_now(), cycle=cycle,
                       index_bytes=len(previous),
                       index_sha256=hashlib.sha256(previous).hexdigest()))
                cycle += 1
                if not full_batch and not set(config.selected_runs) <= accepted:
                    _wait(stop, interval_seconds)
        except BaseException as error:
            _write(directory / 'FAILED.json', {
                'at': _now(), 'cycle': cycle, 'error_type': type(error).__name__,
                'error': str(error), 'automatic_retry': False,
                'native_attempts_and_index_preserved': True})
            raise
