"""Pure verification of a candidate's explicitly retained endpoint admission waits."""
import datetime as dt
import hashlib
import json
import math
from pathlib import PurePosixPath


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def epoch(value):
    stamp = dt.datetime.fromisoformat(value)
    require(stamp.tzinfo is not None, 'capacity_wait_naive_time')
    return stamp.timestamp()


def pinned(row):
    ref, value = row['pin'], row['value']
    raw = encoded(value) + b'\n'
    path = PurePosixPath(ref['path'])
    require(path.is_absolute() and '..' not in path.parts and str(path) == ref['path'],
            'capacity_wait_noncanonical_pin')
    require(type(ref['bytes']) is int and len(raw) == ref['bytes']
            and hashlib.sha256(raw).hexdigest() == ref['sha256'], 'capacity_wait_value_pin_changed')
    return value


def physical(observation, binding):
    paths = observation['paths']
    require(observation['mount'] == binding['mount_identity'], 'capacity_wait_mount_changed')
    require(paths['backing'] == paths['mount_target'] ==
            {'device': binding['scratch_device'], 'inode': binding['backing_inode']},
            'capacity_wait_backing_changed')
    require(paths['output'] == {'device': binding['scratch_device'], 'inode': binding['output_inode']}
            and paths['root_volume']['device'] == binding['root_device'], 'capacity_wait_volume_changed')
    for key, floor in [('output_free_bytes', binding['floor_bytes']),
                       ('root_free_bytes', binding.get('root_floor_bytes', binding['floor_bytes']))]:
        require(type(observation[key]) is int and observation[key] >= floor, 'capacity_wait_physical_floor')


def qualify_capacity_waits(samples, events, declaration, evidence):
    """Return only proven sample/event exceptions; perform no I/O or control action.

    Evidence contains complete existing reporter transitions and exact lease values.
    Raw sample health, alarms, and dispatcher events remain unchanged in the caller.
    """
    guard = declaration['guard']; binding = guard['binding']; capacity_binding = binding['capacity_guard']
    require(declaration['owner']['workers'] == 8 and declaration['owner']['max_full'] == 4,
            'candidate_capacity_wait_only')
    require(capacity_binding['endpoint_pending_seconds'] == 60,
            'capacity_wait_fixed_duration_required')
    process = next(p for p in declaration['processes'] if p['role'] == declaration['owner']['role'])
    owner = {key: process[key] for key in ('pid', 'process_starttime_ticks', 'argv')}
    leases = {}
    for row in evidence['leases']:
        value = pinned(row); key = row['pin']['sha256']
        require(key not in leases, 'duplicate_capacity_wait_lease')
        require(value['schema'] == 'causes.capacity_guard_lease.v1' and value['binding'] == capacity_binding
                and value['candidate'] == owner, 'capacity_wait_lease_binding_changed')
        require(value['guard']['argv'] == capacity_binding['guard_argv'] and value['guard']['pid'] != owner['pid'],
                'capacity_wait_guard_identity_changed')
        require(value['plan']['path'] == capacity_binding['plan_path'], 'capacity_wait_plan_changed')
        require(PurePosixPath(row['pin']['path']).parent == PurePosixPath(capacity_binding['lease_path']).parent
                and PurePosixPath(row['pin']['path']).name == f"lease_{value['sequence']:08d}.json",
                'capacity_wait_lease_path_changed')
        leases[key] = row
    records = []; previous = None; prior_at = None
    for sequence, row in enumerate(evidence['transitions']):
        value = pinned(row); at = epoch(value['at'])
        require(PurePosixPath(row['pin']['path']) == PurePosixPath(guard['directory']) / f'{sequence:08d}.json'
                and value['schema'] == 'causes.storage_guard_transition.v1'
                and value['sequence'] == sequence and value['previous'] == previous,
                'capacity_wait_transition_chain_changed')
        require(value['owner'] == owner and value['binding_sha256'] == digest(binding)
                and value['source_sha256'] == guard['source']['sha256']
                and value['reporting_lock'] == guard['reporting_lock'], 'capacity_wait_transition_binding_changed')
        require(prior_at is None or at >= prior_at, 'capacity_wait_transition_time_regressed')
        require(value['state'] in ('ready', 'floor_wait', 'guard_fault'), 'capacity_wait_transition_state')
        require((value['state'] == 'guard_fault' and type(value['error']) is str
                 and bool(value['error']) and value['observation'] is None)
                or (value['state'] != 'guard_fault' and value['error'] is None
                    and type(value['observation']) is dict), 'capacity_wait_transition_disposition')
        if 'lease' in row:
            embedded = row['lease']; pinned(embedded)
            require(leases.get(embedded['pin']['sha256']) == embedded, 'capacity_wait_transition_lease_missing')
            require(value['observation']['capacity_guard']['lease'] == embedded['pin'],
                    'capacity_wait_transition_lease_changed')
        records.append(row); previous = row['pin']; prior_at = at
    require(records, 'capacity_wait_transition_history_empty')

    def pending(capacity, at, observation, end=None, mono=None, end_mono=None):
        require(capacity['status'] == 'endpoint_pending' and capacity['lease_state'] == 'endpoint_pending'
                and capacity['admission_allowed'] is False, 'capacity_wait_not_pending')
        row = leases[capacity['lease']['sha256']]; lease = row['value']
        require(capacity['lease'] == row['pin'] and capacity['candidate'] == lease['candidate']
                and capacity['guard'] == lease['guard'], 'capacity_wait_lease_identity_changed')
        require(lease['state'] == 'endpoint_pending', 'capacity_wait_lease_state_changed')
        lo, hi = epoch(lease['issued_at']), epoch(lease['expires_at'])
        mlo, mhi = lease['issued_monotonic_seconds'], lease['expires_monotonic_seconds']
        require(all(type(v) in (int, float) and math.isfinite(v) for v in (mlo, mhi))
                and (dt.datetime.fromisoformat(lease['expires_at']) - dt.datetime.fromisoformat(lease['issued_at'])).total_seconds() == 60
                and mhi == mlo + 60, 'capacity_wait_lease_interval_changed')
        require(lo <= at < hi and (end is None or at <= end < hi), 'capacity_wait_expired_or_future')
        if mono is not None:
            require(mlo <= mono < mhi and mono <= end_mono < mhi
                    and abs((at - lo) - (mono - mlo)) <= 1
                    and abs((end - lo) - (end_mono - mlo)) <= 1, 'capacity_wait_clock_or_monotonic_interval')
        physical(observation, binding)
        return row['pin']

    allowed_samples = []; allowed_events = []; used = []
    for sample in samples:
        value = sample['storage_guard']
        if value.get('healthy') and not any(value.get('alarms', {}).values()):
            continue
        # All non-capacity alarms remain disqualifying, even if a lease is valid.
        alarms = value['alarms']
        require(set(alarms) == {'owner_identity_invalid', 'reporting_lock_invalid', 'guard_record_invalid',
                               'guard_not_ready', 'storage_binding_invalid', 'volume_below_floor',
                               'capacity_guard_not_ready'}
                and all(type(flag) is bool for flag in alarms.values()), 'capacity_wait_alarm_schema')
        require(value['healthy'] is False and alarms.get('capacity_guard_not_ready') is True
                and all(flag is False for name, flag in alarms.items() if name != 'capacity_guard_not_ready'),
                'capacity_wait_other_storage_alarm')
        at, end = epoch(sample['at']), epoch(sample['finished_at'])
        require(at <= epoch(value['at']) <= end, 'capacity_wait_sample_observation_time')
        ref = pending(value['capacity_guard'], at, value['fresh_storage'], end,
                      sample['monotonic_seconds'], sample['finished_monotonic_seconds'])
        require(value['fresh_storage']['capacity_guard'] == value['capacity_guard'],
                'capacity_wait_sample_capacity_changed')
        preceding = [row for row in records if epoch(row['value']['at']) <= epoch(value['at'])]
        require(preceding and preceding[-1]['pin'] == value['latest_transition'],
                'capacity_wait_sample_transition_not_latest')
        require(preceding[-1]['value']['state'] != 'guard_fault', 'capacity_wait_sample_fault_transition')
        allowed_samples.append(sample['index']); used.append(ref)
    for event in events:
        if event['event'] != 'disk_floor_wait':
            continue
        require(event['controller_id'] == declaration['owner']['controller_id']
                and event['free_bytes'] == 0 and event['required_bytes'] == binding['floor_bytes'],
                'capacity_wait_event_identity_changed')
        at = epoch(event['at'])
        preceding = [row for row in records if epoch(row['value']['at']) <= at]
        require(preceding, 'capacity_wait_event_transition_missing')
        row = preceding[-1]; value = row['value']
        require(value['state'] == 'floor_wait' and value['error'] is None,
                'capacity_wait_event_not_floor_transition')
        ref = pending(value['observation']['capacity_guard'], at, value['observation'])
        allowed_events.append(digest(event)); used.append(ref)
    return {'sample_indices': allowed_samples, 'event_sha256': allowed_events,
            'lease_pins': [dict(items) for items in sorted({tuple(sorted(ref.items())) for ref in used})],
            'scope': 'Only exact unexpired endpoint_pending; raw health, events and physical alarms preserved.',
            'runtime_actions': False}
