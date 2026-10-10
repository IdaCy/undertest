import datetime as dt
import fcntl
import hashlib
import math
import os
from pathlib import Path
import time

from causes import campaign_handoff as h


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def self_process():
    return {'pid': os.getpid(), 'process_starttime_ticks':
            Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
            'argv': [v.decode() for v in Path('/proc/self/cmdline').read_bytes().split(b'\0') if v]}


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_binding(binding):
    require(binding['schema'] == 'causes.capacity_guard_binding.v1', 'capacity_guard_schema')
    require(type(binding['trial_id']) is str and binding['trial_id'], 'capacity_trial_required')
    for key in ('output_root', 'plan_path', 'lease_path', 'health_lock_path', 'pause_file'):
        h.canonical_path(binding[key])
    require(Path(binding['lease_path']).parent == Path(binding['health_lock_path']).parent,
            'capacity_guard_record_directory_mismatch')
    require(binding['lease_path'] != binding['health_lock_path'], 'distinct_capacity_health_paths_required')
    require(binding['healthy_lease_seconds'] == 45
            and type(binding['healthy_lease_seconds']) is int
            and binding['endpoint_pending_seconds'] == 60
            and type(binding['endpoint_pending_seconds']) is int, 'fixed_capacity_lease_limits')
    require(type(binding['guard_argv']) is list and binding['guard_argv']
            and all(type(v) is str for v in binding['guard_argv'])
            and binding['plan_path'] in binding['guard_argv'], 'capacity_guard_argv_required')
    require(type(binding['required_sources']) is list, 'capacity_guard_sources_required')
    owned = h.checked(binding['owned_startup_barrier'])
    require(owned['output_root'] == binding['output_root'], 'capacity_barrier_output_changed')
    h.barrier_path(owned['marker']['path'])
    require(len({binding['pause_file'], binding['lease_path'], binding['health_lock_path'],
                 owned['marker']['path']}) == 4, 'capacity_control_paths_overlap')
    for source in [binding['guard_source'], *binding['required_sources']]:
        h.checked_bytes(source)
    require(binding['guard_source']['sha256'] == _SOURCE_SHA
            == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'loaded_capacity_guard_source_changed')
    for key, flag, field in (('criteria', 'accepted_prospective_operational_criteria', 'criteria'),
                            ('reference', 'accepted_capacity_reference', 'summary')):
        pair = binding[key]
        h.checked(pair['evidence'])
        review = h.checked(pair['review'])
        require(review.get(flag) is True and review[field] == pair['evidence'],
                'capacity_guard_unaccepted_' + key)
    require(h.checked(binding['reference']['review'])['criteria'] == binding['criteria']['evidence'],
            'capacity_guard_reference_criteria_changed')
    adoption = h.checked(binding['criteria_adoption'])
    require(adoption.get('adopted_prospective_operational_criteria') is True
            and adoption['criteria'] == binding['criteria']['evidence'], 'capacity_guard_criteria_unadopted')
    return owned


def _validate_lease(binding, owner):
    pointer = h.checked(h.pin(binding['lease_path']))
    ref = pointer['record']
    require(Path(ref['path']).parent == Path(binding['lease_path']).parent,
            'capacity_lease_record_outside_directory')
    lease = h.checked(ref)
    require(lease['schema'] == 'causes.capacity_guard_lease.v1'
            and lease['binding'] == binding and lease['candidate'] == owner,
            'capacity_lease_candidate_or_binding_changed')
    require(lease['boot_id'] == boot_id(), 'capacity_lease_boot_changed')
    guard = h.process_identity(lease['guard'])
    h.process_identity(owner)
    require(guard['pid'] != owner['pid'], 'separate_capacity_guard_process_required')
    require(guard['argv'] == binding['guard_argv'], 'capacity_guard_process_changed')
    require(h.stat_identity(binding['health_lock_path']) == lease['health_lock']
            and h.lock_owned_by(binding['health_lock_path'], guard['pid']),
            'capacity_guard_health_lock_missing')
    for source in [binding['guard_source'], *binding['required_sources']]:
        h.checked_bytes(source)
    require(binding['guard_source']['sha256'] == _SOURCE_SHA
            == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'loaded_capacity_guard_source_changed')
    require(lease['plan']['path'] == binding['plan_path'], 'capacity_guard_plan_path_changed')
    plan = h.checked(lease['plan'])
    require(plan['capacity_guard'] == binding and plan['owner_process'] == owner,
            'capacity_guard_plan_identity_changed')
    require(all(plan[key] == binding[key] for key in ('criteria', 'criteria_adoption', 'reference')),
            'capacity_guard_plan_evidence_changed')
    require(type(lease['sequence']) is int and lease['sequence'] >= 0
            and Path(ref['path']).name == f"lease_{lease['sequence']:08d}.json",
            'capacity_lease_sequence_invalid')
    state = lease['state']
    require(state in ('healthy', 'endpoint_pending', 'held'), 'capacity_lease_state_invalid')
    begin, end = lease['issued_monotonic_seconds'], lease['expires_monotonic_seconds']
    require(finite(begin) and finite(end) and 0 <= begin <= end,
            'capacity_lease_monotonic_interval_invalid')
    limit = 45 if state == 'healthy' else 60 if state == 'endpoint_pending' else 0
    require(end == begin + limit, 'capacity_lease_duration_changed')
    issued = dt.datetime.fromisoformat(lease['issued_at'])
    expires = dt.datetime.fromisoformat(lease['expires_at'])
    require(issued.tzinfo is not None and expires.tzinfo is not None
            and (expires - issued).total_seconds() == limit,
            'capacity_lease_wall_interval_invalid')
    wall, mono = dt.datetime.now(dt.timezone.utc), time.monotonic()
    require(issued <= wall and begin <= mono
            and abs((wall - issued).total_seconds() - (mono - begin)) <= 1,
            'capacity_lease_clock_invalid')
    if state != 'held':
        sample = h.checked(lease['sample'])
        require(sample['finished_at'] == lease['issued_at']
                and sample['finished_monotonic_seconds'] == begin,
                'capacity_lease_sample_boundary_changed')
    expired = state != 'held' and (wall >= expires or mono >= end)
    return {'status': 'expired' if expired else state,
            'lease_state': state, 'admission_allowed': state == 'healthy' and not expired,
            'lease': ref, 'guard': guard, 'candidate': owner,
            'age_seconds': mono - begin,
            'reason': 'capacity_lease_expired' if expired else lease['reason']}


def validate_capacity_guard(binding, owner, *, require_healthy=False):
    """Missing or stale external health cannot permit a new claim."""
    try:
        owned = validate_binding(binding)
        if not os.path.lexists(binding['lease_path']):
            h.verify_owned_barrier(binding['owned_startup_barrier'], owned['marker']['path'])
            result = {'status': 'startup_held', 'admission_allowed': False,
                      'construction_allowed': True, 'reason': 'owned_startup_barrier_present'}
        else:
            result = _validate_lease(binding, owner)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        result = {'status': 'blocked', 'admission_allowed': False,
                  'reason': type(error).__name__ + ':' + str(error)}
    require(not require_healthy or result['status'] == 'healthy',
            'current_healthy_capacity_lease_required:' + result['status'])
    return result


class LeaseWriter:
    """Single owner, durable records, atomic pointer, no renewal after failure."""

    def __init__(self, binding, plan_ref, owner):
        validate_binding(binding)
        self.binding, self.plan_ref, self.owner = binding, plan_ref, owner
        plan = h.checked(plan_ref)
        require(plan_ref['path'] == binding['plan_path'] and plan['capacity_guard'] == binding
                and plan['owner_process'] == owner, 'capacity_writer_plan_changed')
        require(all(plan[key] == binding[key] for key in ('criteria', 'criteria_adoption', 'reference')),
                'capacity_writer_evidence_changed')
        self.process = self_process()
        h.process_identity(owner)
        require(self.process['argv'] == binding['guard_argv'], 'capacity_writer_argv_changed')
        for source in [binding['guard_source'], *binding['required_sources']]:
            h.checked_bytes(source)
        require(binding['guard_source']['sha256'] == _SOURCE_SHA, 'capacity_writer_source_changed')
        self.directory = h.new_record_directory(Path(binding['lease_path']).parent)
        self.lock = Path(binding['health_lock_path']).open('xb')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fsync(self.lock.fileno())
        h.sync_directory(self.directory)
        self.lock_identity = h.stat_identity(binding['health_lock_path'])
        self.sequence, self.failed, self.previous = 0, False, None

    def publish(self, state, sample_ref, reason=None):
        require(not self.failed and not self.lock.closed, 'capacity_writer_already_failed')
        try:
            require(state in ('healthy', 'endpoint_pending', 'held'), 'invalid_capacity_state')
            h.checked(self.plan_ref)
            h.process_identity(self.owner)
            h.process_identity(self.process)
            require(h.lock_owned_by(self.binding['health_lock_path'], self.process['pid']),
                    'capacity_writer_lock_lost')
            require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA,
                    'capacity_writer_source_changed')
            sample = h.checked(sample_ref)
            wall = dt.datetime.fromisoformat(sample['finished_at'])
            mono = sample['finished_monotonic_seconds']
            limit = 45 if state == 'healthy' else 60 if state == 'endpoint_pending' else 0
            require(finite(mono) and wall.tzinfo is not None, 'capacity_sample_time_invalid')
            record = {'schema': 'causes.capacity_guard_lease.v1', 'sequence': self.sequence,
                      'previous': self.previous, 'binding': self.binding,
                      'candidate': self.owner, 'guard': self.process, 'plan': self.plan_ref,
                      'health_lock': self.lock_identity, 'boot_id': boot_id(),
                      'state': state, 'reason': reason, 'sample': sample_ref,
                      'issued_at': wall.isoformat(), 'expires_at': (wall + dt.timedelta(seconds=limit)).isoformat(),
                      'issued_monotonic_seconds': mono, 'expires_monotonic_seconds': mono + limit}
            ref = h.save_new(self.directory, f'lease_{self.sequence:08d}.json', record)
            temp = h.save_new(self.directory, f'pointer_{self.sequence:08d}.json', {'record': ref})
            os.replace(temp['path'], self.binding['lease_path'])
            h.sync_directory(self.directory)
            self.previous, self.sequence = ref, self.sequence + 1
            return ref
        except BaseException:
            self.failed = True
            self.close()
            raise

    def close(self):
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def hold_admission(binding, pause_path, record_directory, reasons):
    """Create only the bound role-local pause; never remove or adopt an existing marker."""
    require(pause_path == binding['pause_file'], 'capacity_pause_scope_changed')
    path = h.canonical_path(pause_path)
    record = {'schema': 'causes.capacity_guard_hold.v1', 'at': h.now(),
              'trial_id': binding['trial_id'], 'output_root': binding['output_root'],
              'guard': self_process(), 'reasons': list(reasons), 'automatic_resume': False}
    intent = h.save_new(record_directory, 'HOLD_INTENT.json', record)
    try:
        with path.open('xb') as stream:
            stream.write(h.encoded({'intent': intent}) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        h.sync_directory(path.parent)
        result = {'created': True, 'marker': h.pin(path), 'stat': h.stat_identity(path)}
    except FileExistsError:
        result = {'created': False, 'existing_marker_preserved': True}
    return h.save_new(record_directory, 'HOLD_RESULT.json', {'intent': intent, **result})
