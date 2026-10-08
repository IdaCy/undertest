from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from collections.abc import Callable
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil

from causes import campaign_handoff as h
from causes.campaign_dispatcher import sha, validate_job


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


TERMINAL_EVENTS = {'episode_finished', 'unresolved_current_attempt', 'existing_completed_summary',
                   'unresolved_prior_attempt', 'unresolved_completed_evidence',
                   'unresolved_identity_conflict'}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def absolute(value):
    require(type(value) is str, 'storage_path_string_required')
    path = PurePosixPath(value)
    require(path.is_absolute() and str(path) == value and '..' not in path.parts,
            'canonical_storage_path_required')
    return path


def unique_ids(values):
    require(type(values) is list, 'storage_ID_list_required')
    require(all(type(v) is str and v and PurePosixPath(v).name == v and v not in ('.', '..')
                for v in values), 'invalid_storage_run_ID')
    require(len(set(values)) == len(values), 'duplicate_storage_run_ID')
    return set(values)


def partition_fixed_jobs(jobs, config, expected_roots, histories, unavailable_ids):
    """Compose a queue from sealed, retired scopes; perform no runtime action."""
    roster = {validate_job(job): job for job in jobs}
    require(len(roster) == len(jobs) and roster, 'unique_nonempty_frozen_roster_required')
    require(type(expected_roots) is list and len(set(expected_roots)) == len(expected_roots)
            and expected_roots, 'explicit_prior_root_inventory_required')
    for root in expected_roots:
        absolute(root)
    require(len(histories) == len(expected_roots)
            and {row['root'] for row in histories} == set(expected_roots),
            'complete_prior_root_inventory_required')
    unavailable = unique_ids(unavailable_ids)
    require(unavailable <= set(roster), 'unavailable_ID_outside_frozen_roster')
    locations, attempted, completed = {}, set(), set()
    evidence = []
    for history in histories:
        require(history['retired'] is True and type(history['active_claims']) is int
                and history['active_claims'] == 0
                and history['output_lock_free'] is True, 'prior_owner_not_retired')
        root = history['root']
        directories = unique_ids(history['run_directories']) & set(roster)
        final = unique_ids(history['generated_ids'])
        require(final <= directories, 'generated_ID_without_original_destination')
        claims, terminal_ids = {}, set()
        for event in history['journal']:
            if event.get('run_id') not in roster:
                continue
            rid = event['run_id']
            if event.get('event') in TERMINAL_EVENTS:
                if 'job_sha256' in event:
                    require(event['job_sha256'] == sha(roster[rid]), 'prior_terminal_input_changed')
                terminal_ids.add(rid)
                continue
            if event.get('event') != 'claimed':
                continue
            require(event['job_sha256'] == sha(roster[rid])
                    and event['config_sha256'] == sha(config), 'prior_claim_input_changed')
            require(rid not in claims, 'repeated_prior_durable_claim')
            claims[rid] = event
        here = directories | set(claims) | terminal_ids
        for rid in here:
            require(rid not in locations, 'frozen_ID_attempted_in_multiple_roots')
            locations[rid] = root
        attempted |= here
        completed |= final
        evidence.append({'root': root, 'history_sha256': sha(history),
                         'attempted_IDs': sorted(here), 'generated_IDs': sorted(final),
                         'terminal_without_claim_or_directory': sorted(terminal_ids - directories - set(claims))})
    require(not attempted & unavailable, 'attempted_unavailable_disposition_overlap')
    unresolved = attempted - completed
    never_claimed = set(roster) - attempted - unavailable
    selected = [copy.deepcopy(job) for job in jobs
                if job['allocation']['run_id'] in never_claimed]
    return {'schema': 'causes.storage_fixed_continuation.v1',
            'frozen_jobs_sha256': sha(jobs), 'config_sha256': sha(config),
            'history_scopes': evidence, 'jobs': selected,
            'jobs_sha256': sha(selected),
            'partition': {'generated': sorted(completed),
                          'administrative_unavailable': sorted(unavailable),
                          'prior_attempted_unresolved': sorted(unresolved),
                          'never_claimed': sorted(never_claimed)},
            'preserves_original_job_order': True, 'replay_authorized': False}


def fixed_custody_join(rows, frozen_ids_by_model, registered_roots):
    """Join original IDs through an explicit additive output-root registry."""
    require(set(frozen_ids_by_model) == set(registered_roots) and frozen_ids_by_model,
            'exact_fixed_root_registry_models_required')
    owners, roots, output = {}, {}, {}
    for model, values in frozen_ids_by_model.items():
        ids = unique_ids(values)
        require(not set(owners) & ids, 'frozen_ID_shared_between_models')
        owners.update({rid: model for rid in ids})
        values = registered_roots[model]
        require(type(values) is list and values and len(set(values)) == len(values),
                'explicit_distinct_fixed_roots_required')
        roots[model] = set(values)
        for value in values:
            path = PurePosixPath(value)
            require(not path.is_absolute() and str(path) == value and '..' not in path.parts
                    and len(path.parts) == 3 and path.parts[0] == 'runs'
                    and path.parts[-1] == model, 'canonical_fixed_root_required')
        output[model] = {}
    for row in rows:
        path = PurePosixPath(row['run'])
        require(not path.is_absolute() and str(path) == row['run'] and '..' not in path.parts,
                'canonical_custody_run_required')
        model = owners.get(path.name)
        if model is None:
            continue
        require(str(path.parent) in roots[model], 'original_ID_in_unregistered_root')
        require(path.name not in output[model], 'multiple_custody_rows_for_original_ID')
        output[model][path.name] = copy.deepcopy(row)
    return output


def drain_progress(events, controller_id):
    """Verify that an acknowledged owner admitted no work after its pause."""
    require(type(controller_id) is str and controller_id, 'exact_controller_ID_required')
    rows = [row for row in events if row.get('controller_id') == controller_id]
    paused = False
    claims, finals = set(), set()
    for row in rows:
        event = row['event']
        if event == 'pause_seen':
            require(row.get('admission_stopped') is True, 'pause_acknowledgement_required')
            paused = True
        elif event == 'claimed':
            require(not paused, 'claim_after_pause_acknowledgement')
            require(row['run_id'] not in claims, 'repeated_owner_claim')
            require(row['run_id'] not in finals, 'claim_after_terminal_disposition')
            claims.add(row['run_id'])
        elif event in TERMINAL_EVENTS:
            require(row['run_id'] not in finals, 'repeated_owner_terminal')
            finals.add(row['run_id'])
    require(paused, 'owner_has_not_acknowledged_pause')
    return {'claims': len(claims), 'active_claims': len(claims - finals),
            'pause_acknowledged': True, 'new_claims_after_pause': 0}


def validate_pause_routing(routing, drain_marker, controls_present, *, admitting_role):
    """Validate explicit control succession; an old pause is never silently ignored."""
    require(routing['schema'] in ('causes.storage_pause_routing.v1',
                                  'causes.storage_pause_routing.v2')
            and routing['root_accepted'] is True, 'accepted_pause_routing_required')
    require(drain_marker['origin'] == 'storage_transition'
            and drain_marker['transition_id'] == routing['transition_id']
            and drain_marker['pin'] == routing['drain_marker'], 'owned_transition_marker_required')
    require(set(routing['successor_local_pauses']) == {'mi', 'texas'},
            'both_successor_pause_controls_required')
    require(admitting_role in routing['successor_local_pauses'], 'explicit_admitting_role_required')
    old = routing['drain_marker']['path']
    successor_global = routing['successor_global_pause']
    retained = retained_owned_pauses(routing)
    paths = [old, successor_global, *routing['successor_local_pauses'].values(), *retained]
    require(len(set(paths)) == len(paths), 'distinct_storage_pause_controls_required')
    for path in paths:
        absolute(path)
    require(type(routing['user_pause_fanout']) is list
            and len(routing['user_pause_fanout']) == len(paths)
            and set(routing['user_pause_fanout']) == set(paths), 'incomplete_user_pause_coverage')
    require(set(controls_present) == set(paths)
            and all(type(value) is bool for value in controls_present.values()),
            'exact_pause_observations_required')
    require(controls_present[old] is True, 'old_owner_drain_marker_missing')
    require(all(controls_present[path] for path in retained),
            'retained_owned_pause_missing')
    require(not controls_present[successor_global]
            and not controls_present[routing['successor_local_pauses'][admitting_role]],
            'user_or_successor_pause_blocks_admission')
    return {'transition_id': routing['transition_id'],
            'old_owner_drain_marker_preserved': True,
            'admitting_role': admitting_role,
            'successor_global_pause': successor_global,
            'user_pause_fanout': list(routing['user_pause_fanout'])}


def retained_owned_pauses(routing):
    """Preserve reviewed MI drain markers through repeated owner succession."""
    if routing['schema'] == 'causes.storage_pause_routing.v1':
        require('retained_owned_pauses' not in routing and 'predecessor_routing' not in routing,
                'pause_ancestry_requires_explicit_schema')
        return []
    require(routing['schema'] == 'causes.storage_pause_routing.v2', 'pause_ancestry_schema')
    previous = h.checked(routing['predecessor_routing'])
    require(previous['schema'] in ('causes.storage_pause_routing.v1',
                                   'causes.storage_pause_routing.v2')
            and previous['root_accepted'] is True, 'accepted_previous_pause_routing_required')
    require(previous['schema'] != 'causes.storage_pause_routing.v1'
            or ('retained_owned_pauses' not in previous and 'predecessor_routing' not in previous),
            'previous_pause_ancestry_schema_changed')
    for key in ('transition_id', 'drain_marker', 'successor_global_pause'):
        require(routing[key] == previous[key], 'inherited_pause_control_changed')
    require(routing['successor_local_pauses']['texas'] == previous['successor_local_pauses']['texas']
            and routing['successor_local_pauses']['mi'] != previous['successor_local_pauses']['mi'],
            'mi_only_pause_succession_required')
    pairs = routing['retained_owned_pauses']
    inherited = previous.get('retained_owned_pauses', [])
    require(type(pairs) is list and 1 <= len(pairs) <= 32
            and len(pairs) == len(inherited) + 1 and pairs[:-1] == inherited,
            'complete_ordered_pause_ancestry_required')
    paths = []
    for pair in pairs:
        value, review = h.checked(pair['evidence']), h.checked(pair['review'])
        require(review.get('accepted_retired_owned_pause') is True
                and review.get('ownership') == pair['evidence']
                and value['schema'] == 'causes.retired_owned_pause.v1'
                and value['role'] == 'mi' and value['owner_retired'] is True,
                'unaccepted_retired_owned_pause')
        marker = value['marker']
        h.checked_bytes(marker)
        require(h.stat_identity(marker['path']) == value['stat'], 'retained_owned_pause_changed')
        paths.append(marker['path'])
    require(paths[-1] == previous['successor_local_pauses']['mi']
            and value['predecessor_routing'] == routing['predecessor_routing'],
            'retired_pause_predecessor_changed')
    require(type(previous['user_pause_fanout']) is list
            and len(set(previous['user_pause_fanout'])) == len(previous['user_pause_fanout'])
            and set(routing['user_pause_fanout']) == set(previous['user_pause_fanout'])
                | {routing['successor_local_pauses']['mi']}, 'previous_pause_fanout_not_preserved')
    return paths


def validate_mixed_phase(registration, observations):
    """Check sealed phase observations; callers establish freshness and provenance."""
    phases = {
        'texas_waiting': {'old_mi': 'draining', 'old_texas': 'retired',
                          'new_texas': 'waiting'},
        'texas_running_mi_draining': {'old_mi': 'draining', 'old_texas': 'retired',
                                     'new_texas': 'running'},
        'texas_waiting_mi_retired': {'old_mi': 'retired', 'old_texas': 'retired',
                                    'new_texas': 'waiting'},
        'texas_running_mi_retired': {'old_mi': 'retired', 'old_texas': 'retired',
                                    'new_texas': 'running'},
        'mi_waiting': {'old_mi': 'retired', 'old_texas': 'retired',
                       'new_texas': 'running', 'new_mi': 'waiting'},
        'successors_running': {'old_mi': 'retired', 'old_texas': 'retired',
                               'new_texas': 'running', 'new_mi': 'running'},
    }
    require(registration['schema'] == 'causes.storage_controller_phase.v1', 'storage_phase_schema')
    wanted = phases.get(registration['phase'])
    require(wanted is not None and set(registration['owners']) == set(wanted)
            and set(observations) == set(wanted), 'exact_storage_phase_owners_required')
    outputs = [row['output_root'] for row in registration['owners'].values()]
    require(len(set(outputs)) == len(outputs), 'distinct_storage_owner_outputs_required')
    result = {}
    for role, state in wanted.items():
        expected, observed = registration['owners'][role], observations[role]
        process = expected['process']
        require(type(process['pid']) is int and process['pid'] > 0
                and type(process['process_starttime_ticks']) is str
                and process['process_starttime_ticks'].isdigit()
                and int(process['process_starttime_ticks']) > 0
                and type(process['argv']) is list and process['argv']
                and all(type(v) is str for v in process['argv']), 'invalid_storage_process_identity')
        require(observed['process'] == expected['process']
                and observed['output_root'] == expected['output_root'], 'storage_owner_identity_changed')
        require(type(observed['active_claims']) is int and observed['active_claims'] >= 0,
                'invalid_storage_active_claims')
        absolute(expected['output_root'])
        if state == 'retired':
            require(observed['live'] is False and observed['lock_owner'] is None
                    and observed['active_claims'] == 0, 'old_storage_owner_not_retired')
        else:
            require(observed['live'] is True
                    and observed['lock_owner'] == expected['process']['pid'], 'storage_owner_not_live_lock_holder')
            require(observed['queue_identity'] == expected['queue_identity'], 'storage_queue_identity_changed')
            if state == 'draining':
                progress = drain_progress(observed['journal'], expected['controller_id'])
                require(progress['active_claims'] == observed['active_claims'], 'draining_claim_count_changed')
            else:
                require(observed['barrier_present'] is (state == 'waiting'), 'storage_barrier_state_changed')
                if state == 'waiting':
                    require(type(observed['claims']) is int and observed['claims'] == 0
                            and observed['active_claims'] == 0
                            and type(observed['persisted_blocks']) is int
                            and observed['persisted_blocks'] == 0, 'waiting_storage_owner_issued_work')
        result[role] = state
    return {'phase': registration['phase'], 'roles': result,
            'processes_restarted': False, 'runtime_actions': False}


def mount_rows(raw):
    result = []
    for line in raw.splitlines():
        fields = line.split()
        separator = fields.index('-')
        require(separator >= 6 and len(fields) >= separator + 3, 'malformed_storage_mountinfo')
        unescape = lambda value: value.replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')
        result.append({'mount_id': fields[0], 'device': fields[2],
                       'root': unescape(fields[3]), 'target': unescape(fields[4]),
                       'fstype': fields[separator + 1]})
    return result


def storage_observation(binding):
    names = ('backing', 'mount_target', 'output', 'root_volume')
    paths = {key: h.canonical_path(binding[key]) for key in names}
    require(all(path.is_dir() for path in paths.values()), 'storage_directories_required')
    stats = {key: path.stat() for key, path in paths.items()}
    rows = mount_rows(Path('/proc/self/mountinfo').read_text())
    mounts = [row for row in rows if row['target'] == str(paths['mount_target'])]
    require(len(mounts) == 1, 'exact_storage_mount_required')
    return {'paths': {key: {'device': str(value.st_dev), 'inode': str(value.st_ino)}
                      for key, value in stats.items()},
            'mount': mounts[0],
            'output_free_bytes': shutil.disk_usage(paths['output']).free,
            'root_free_bytes': shutil.disk_usage(paths['root_volume']).free}


def storage_control_observation(binding):
    require(binding['schema'] == 'causes.storage_admission_controls.v1',
            'storage_admission_control_schema')
    routing = h.checked(binding['pause_routing'])
    marker = h.checked(binding['drain_marker'])
    h.checked_bytes(marker['pin'])
    actual_stat = h.stat_identity(marker['pin']['path'])
    require(actual_stat == marker['stat'], 'owned_storage_drain_marker_changed')
    present = {path: os.path.lexists(path) for path in routing['user_pause_fanout']}
    decision = validate_pause_routing(routing, marker, present, admitting_role=binding['role'])
    return {'pause_routing': binding['pause_routing'], 'drain_marker': binding['drain_marker'],
            'marker': marker['pin'], 'marker_stat': actual_stat,
            'controls_present': present, 'decision': decision}


def validate_capacity_guard(binding, owner, *, require_healthy=False):
    from causes.campaign_capacity_guard import validate_capacity_guard as validate
    return validate(binding, owner, require_healthy=require_healthy)


@dataclass
class StorageGuard:
    binding: dict
    observe: Callable = storage_observation
    report: Callable | None = None
    capacity_owner: dict | None = None
    last_observation: dict | None = field(default=None, init=False)
    last_error: str | None = field(default=None, init=False)

    def __post_init__(self):
        self.binding = copy.deepcopy(self.binding)
        b = self.binding
        require(callable(self.observe) and (self.report is None or callable(self.report)),
                'storage_callbacks_required')
        require(b['schema'] == 'causes.prospective_storage_binding.v1', 'storage_binding_schema')
        paths = {key: absolute(b[key]) for key in ('backing', 'mount_target', 'output', 'root_volume')}
        require(paths['output'].is_relative_to(paths['mount_target'])
                and paths['output'] != paths['mount_target'], 'output_must_be_below_new_mount')
        require(not paths['backing'].is_relative_to(paths['mount_target'])
                and not paths['mount_target'].is_relative_to(paths['backing']), 'storage_mount_paths_overlap')
        require(type(b['floor_bytes']) is int and b['floor_bytes'] > 0, 'positive_storage_floor_required')
        root_floor = b.get('root_floor_bytes', b['floor_bytes'])
        require(type(root_floor) is int and 0 < root_floor <= b['floor_bytes'],
                'valid_independent_root_floor_required')
        for key in ('scratch_device', 'backing_inode', 'output_inode', 'root_device'):
            require(type(b[key]) is str and b[key].isdigit() and int(b[key]) > 0,
                    'exact_decimal_storage_identity_required')
        require(b['scratch_device'] != b['root_device'], 'separate_storage_device_required')
        require(type(b['mount_identity']) is dict and set(b['mount_identity']) ==
                {'mount_id', 'device', 'root', 'target', 'fstype'}, 'exact_mount_identity_required')
        require(b['mount_identity']['target'] == b['mount_target'], 'mount_target_identity_changed')
        if 'capacity_guard' in b:
            require(b['capacity_guard']['output_root'] == b['output'], 'capacity_guard_output_changed')

    def _check(self, allow_startup=True):
        b = self.binding
        observation = copy.deepcopy(self.observe(b))
        if 'admission_control' in b:
            observation['admission_control'] = storage_control_observation(b['admission_control'])
        require(observation['mount'] == b['mount_identity'], 'storage_mount_identity_changed')
        paths = observation['paths']
        require(paths['backing'] == paths['mount_target'] ==
                {'device': b['scratch_device'], 'inode': b['backing_inode']}, 'storage_backing_identity_changed')
        require(paths['output'] == {'device': b['scratch_device'], 'inode': b['output_inode']}
                and paths['root_volume']['device'] == b['root_device'], 'storage_volume_identity_changed')
        free = [observation['output_free_bytes'], observation['root_free_bytes']]
        require(all(type(value) is int and value >= 0 for value in free), 'invalid_storage_free_bytes')
        if 'capacity_guard' in b:
            from causes.campaign_capacity_guard import self_process
            capacity = validate_capacity_guard(b['capacity_guard'], self.capacity_owner or self_process())
            observation['capacity_guard'] = capacity
            if not capacity['admission_allowed'] and not (allow_startup and capacity.get('construction_allowed', False)):
                self.last_observation, self.last_error = copy.deepcopy(observation), None
                return 0
        self.last_observation, self.last_error = copy.deepcopy(observation), None
        if 'root_floor_bytes' in b and free[1] >= b['root_floor_bytes']:
            return free[0]
        return min(free)

    def check(self, *, allow_startup=True):
        try:
            free = self._check(allow_startup)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.last_observation = None
            self.last_error = type(error).__name__ + ':' + str(error)
            if self.report is not None:
                self.report({'state': 'guard_fault', 'error': self.last_error, 'observation': None})
            raise
        if self.report is not None:
            self.report({'state': 'ready' if free >= self.binding['floor_bytes'] else 'floor_wait',
                         'error': None, 'observation': self.last_observation})
        return free

    def __call__(self):
        try:
            return self.check(allow_startup=False)
        except (OSError, ValueError, KeyError, TypeError):
            return 0


class GuardReportingError(RuntimeError):
    pass


class GuardJournal:
    """Durable state transitions plus a fail-closed reporting-health lock."""

    def __init__(self, directory, binding, owner):
        require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA,
                'loaded_storage_source_changed')
        require(owner['pid'] == os.getpid(), 'guard_reporter_must_be_owner_process')
        h.process_identity(owner)
        self.owner = copy.deepcopy(owner)
        self.binding_sha256 = sha(binding)
        self.directory = h.new_record_directory(directory)
        self.lock_path = self.directory / 'reporting.lock'
        self.lock = self.lock_path.open('xb')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fsync(self.lock.fileno())
        h.sync_directory(self.directory)
        self.lock_identity = h.stat_identity(self.lock_path)
        self.source_sha256 = _SOURCE_SHA
        self.previous, self.last_state, self.sequence, self.broken = None, None, 0, False

    def __call__(self, event):
        if self.broken:
            raise GuardReportingError('guard_reporting_previously_failed')
        try:
            require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == self.source_sha256 == _SOURCE_SHA,
                    'loaded_storage_source_changed')
            require(not self.lock.closed and h.lock_owned_by(self.lock_path, self.owner['pid']),
                    'guard_reporting_health_lock_missing')
            require(event['state'] in ('ready', 'floor_wait', 'guard_fault'), 'invalid_guard_state')
            key = (event['state'], event['error'])
            if key == self.last_state:
                return
            record = {'schema': 'causes.storage_guard_transition.v1', 'at': h.now(),
                      'sequence': self.sequence, 'previous': self.previous,
                      'owner': self.owner, 'binding_sha256': self.binding_sha256,
                      'source_sha256': self.source_sha256, 'reporting_lock': self.lock_identity,
                      **copy.deepcopy(event)}
            ref = h.save_new(self.directory, f'{self.sequence:08d}.json', record)
            self.previous, self.last_state = ref, key
            self.sequence += 1
        except Exception as error:
            self.broken = True
            if not self.lock.closed:
                fcntl.flock(self.lock, fcntl.LOCK_UN)
            raise GuardReportingError('guard_reporting_failed_admission_closed') from error

    def close(self):
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def observe_guard(directory, binding, owner, expected_lock, *,
                  observe_storage=storage_observation, max_records=10000):
    """Fresh local observation; old ready transitions alone never prove health."""
    alarms = {'owner_identity_invalid': False, 'reporting_lock_invalid': False,
              'guard_record_invalid': False, 'guard_not_ready': False,
              'storage_binding_invalid': False, 'volume_below_floor': False}
    result = {'at': h.now(), 'alarms': alarms, 'latest_transition': None,
              'fresh_storage': None, 'healthy': False}
    canonical_directory = None
    try:
        h.process_identity(owner)
    except (OSError, ValueError, KeyError, TypeError):
        alarms['owner_identity_invalid'] = True
    try:
        canonical_directory = h.canonical_path(directory)
        lock_path = canonical_directory / 'reporting.lock'
        require(h.stat_identity(lock_path) == expected_lock
                and h.lock_owned_by(lock_path, owner['pid']), 'guard_reporting_lock_changed')
    except (OSError, ValueError, KeyError, TypeError):
        alarms['reporting_lock_invalid'] = True
    try:
        require(canonical_directory is not None, 'invalid_guard_record_directory')
        paths = sorted(canonical_directory.glob('[0-9]*.json'))
        require(0 < len(paths) <= max_records, 'bounded_nonempty_guard_history_required')
        previous = None
        source = _SOURCE_SHA
        require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == source,
                'loaded_storage_source_changed')
        for sequence, path in enumerate(paths):
            require(path.name == f'{sequence:08d}.json', 'guard_history_sequence_gap')
            ref = h.pin(path)
            record = h.checked(ref)
            require(record['schema'] == 'causes.storage_guard_transition.v1'
                    and record['sequence'] == sequence and record['previous'] == previous,
                    'guard_history_identity_changed')
            require(record['owner'] == owner and record['binding_sha256'] == sha(binding)
                    and record['source_sha256'] == source and record['reporting_lock'] == expected_lock,
                    'guard_history_binding_changed')
            require(record['state'] in ('ready', 'floor_wait', 'guard_fault'), 'guard_history_state_invalid')
            require((record['state'] == 'guard_fault' and type(record['error']) is str
                     and record['error'] and record['observation'] is None)
                    or (record['state'] != 'guard_fault' and record['error'] is None
                        and type(record['observation']) is dict), 'guard_history_disposition_invalid')
            at = dt.datetime.fromisoformat(record['at'])
            require(at.tzinfo is not None and at <= dt.datetime.now(dt.timezone.utc),
                    'guard_history_timestamp_invalid')
            previous = ref
        result['latest_transition'] = previous
        alarms['guard_not_ready'] = record['state'] != 'ready'
        if 'capacity_guard' in binding and record['state'] == 'floor_wait':
            old = record['observation']
            capacity_wait = old.get('capacity_guard', {}).get('status') in ('endpoint_pending', 'expired')
            physical_ok = (old['output_free_bytes'] >= binding['floor_bytes']
                           and old['root_free_bytes'] >= binding.get('root_floor_bytes', binding['floor_bytes']))
            if capacity_wait and physical_ok:
                alarms['guard_not_ready'] = False
    except (OSError, ValueError, KeyError, TypeError):
        alarms['guard_record_invalid'] = True
    try:
        guard = StorageGuard(binding, observe_storage, capacity_owner=owner)
        free = guard.check()
        result['fresh_storage'] = guard.last_observation
        alarms['volume_below_floor'] = free < binding['floor_bytes']
        if 'capacity_guard' in binding:
            current = guard.last_observation
            result['capacity_guard'] = current['capacity_guard']
            alarms['capacity_guard_not_ready'] = current['capacity_guard']['status'] not in ('healthy', 'startup_held')
            alarms['volume_below_floor'] = (current['output_free_bytes'] < binding['floor_bytes']
                or current['root_free_bytes'] < binding.get('root_floor_bytes', binding['floor_bytes']))
    except (OSError, ValueError, KeyError, TypeError):
        alarms['storage_binding_invalid'] = True
    result['healthy'] = not any(alarms.values())
    result['scope'] = 'Fresh owner, reporting-lock and storage checks plus bound durable transitions; transition age alone is not an alarm.'
    return result


def guarded_dispatcher(dispatcher_class, *, storage_binding, config, output,
                       guard_reporter=None, **kwargs):
    guard = StorageGuard(storage_binding, report=guard_reporter)
    require(str(absolute(output)) == storage_binding['output'], 'dispatcher_output_binding_changed')
    require(config.get('dispatcher', {}).get('disk_floor_bytes') == storage_binding['floor_bytes'],
            'dispatcher_storage_floor_changed')
    require('free_bytes' not in kwargs, 'storage_guard_cannot_be_overridden')
    require(guard.check() >= storage_binding['floor_bytes'], 'storage_admission_floor_not_met')
    return dispatcher_class(config=config, output=output, free_bytes=guard, **kwargs), guard
