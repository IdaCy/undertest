"""Admission checks for a finite supplement sharing a provider with actors.

This module performs no capture, launch, model call, control mutation, retry or
capacity promotion. A reviewed observer must retain fresh process, owner, lock,
guard, metrics and resource evidence. Exact declared identities and byte-pinned
plans complement that evidence; this validator cannot discover unknown owners.
The native supplement history, storage and queue semantics remain unchanged.
"""
import datetime as dt
import math
import json

from causes import campaign_supplement as core
from causes import campaign_supplement_launch as launch
from causes.campaign_storage import StorageGuard

SCHEMA = 'causes.supplement_co_residency_contract.v1'
OBSERVATION_SCHEMA = 'causes.supplement_co_residency_observation.v1'
CHECKS = ('complete_owner_inventory', 'actor_processes', 'actor_locks',
          'actor_guards', 'provider_metrics', 'execution_resources',
          'client_resources')


def _require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def _same(left, right, reason):
    _require(core.encoded(left) == core.encoded(right), reason)


def _integer(value, minimum, reason):
    _require(type(value) is int and value >= minimum, reason)
    return value


def _process(value):
    _require(type(value) is dict and set(value) == {'pid', 'process_starttime_ticks', 'argv'},
             'co_resident_process_fields')
    _integer(value['pid'], 1, 'co_resident_pid')
    _require(type(value['process_starttime_ticks']) is str
             and value['process_starttime_ticks'].isdigit()
             and int(value['process_starttime_ticks']) > 0, 'co_resident_start_ticks')
    _require(type(value['argv']) is list and value['argv']
             and all(type(arg) is str and '\x00' not in arg for arg in value['argv']),
             'co_resident_argv')


def _references(refs, reason):
    _require(type(refs) is list and refs
             and len({ref['path'] for ref in refs}) == len(refs), reason)
    for ref in refs:
        core.pinned(ref)


def validate_contract(contract, policy):
    """Check immutable bindings; this does not grant runtime authority."""
    _require(contract['schema'] == SCHEMA, 'co_resident_contract_schema')
    _require(contract['supplement_clients'] == policy['max_clients'] == 1
             and type(contract['supplement_clients']) is int
             and type(policy['max_clients']) is int,
             'co_resident_one_client_only')
    _same(contract['provider_identity'], policy['runtime_identity'],
          'co_resident_provider_binding')
    _require(type(contract['provider_host']) is str and contract['provider_host'],
             'co_resident_provider_host')
    _process(contract['provider_process'])
    cap = _integer(contract['server_max_sequences'], 1, 'co_resident_sequence_cap')
    age = contract['maximum_observation_age_seconds']
    _require(type(age) is int and 0 < age <= 30, 'co_resident_observation_age_limit')
    _integer(contract['baseline_preemptions'], 0, 'co_resident_preemption_baseline')
    _references(contract['required_pins'], 'co_resident_required_pins')
    _require(contract['owner_inventory'] and type(contract['owner_inventory']) is list,
             'co_resident_owner_inventory_required')
    actors = contract['actors']
    _require(type(actors) is list and actors
             and len({a['id'] for a in actors}) == len(actors), 'co_resident_actor_roster')
    donors = {scope['id']: scope for scope in policy['donors']}
    total_workers = 0
    for actor in actors:
        _require(type(actor['id']) is str and actor['id'], 'co_resident_actor_id')
        plan = json.loads(core.pinned(actor['plan']))
        _require(actor['plan'] in contract['required_pins'], 'co_resident_plan_not_pinned')
        _integer(actor['workers'], 1, 'co_resident_actor_workers')
        _same({key: plan[key] for key in ('execution_host', 'provider_host', 'workers', 'output')},
              {key: actor[key] for key in ('execution_host', 'provider_host', 'workers', 'output')},
              'co_resident_actor_plan_changed')
        _require(actor['provider_host'] == contract['provider_host'],
                 'co_resident_actor_provider_host')
        _same(plan['provider_process'], contract['provider_process'], 'co_resident_actor_provider_process')
        _process(actor['process'])
        guard = actor['guard_binding']
        _same(guard['binding'], plan['storage_binding'], 'co_resident_actor_storage_binding')
        _require(not ({'admission_control', 'capacity_guard'} & set(guard['binding'])),
                 'co_resident_plain_storage_guard_required')
        _require(guard['reporter_source'] in contract['required_pins'],
                 'co_resident_guard_source_not_pinned')
        _require(str(core.canonical(guard['directory'])) == guard['directory']
                 and type(guard['reporting_lock']['st_dev']) is int
                 and type(guard['reporting_lock']['st_ino']) is int, 'co_resident_guard_identity')
        lock = actor['lock']
        _require(type(lock) is dict and set(lock) == {'path', 'device', 'inode'}
                 and str(core.canonical(lock['path'])) == lock['path'], 'co_resident_lock_identity')
        _integer(lock['device'], 0, 'co_resident_lock_device')
        _integer(lock['inode'], 1, 'co_resident_lock_inode')
        donor = donors[actor['journal_scope_id']]
        _require(donor['mode'] == 'live_prefix' and 'history_progression' not in donor,
                 'co_resident_actor_history_must_hold_on_fault')
        _same(donor['identity'], actor['journal_identity'], 'co_resident_actor_history_identity')
        _same({key: donor['identity'][key] for key in ('execution_host', 'output', 'process')},
              {key: actor[key] for key in ('execution_host', 'output', 'process')},
              'co_resident_actor_journal_scope_changed')
        matching = [row for row in contract['owner_inventory'] if row['id'] == actor['id']]
        _require(len(matching) == 1, 'co_resident_actor_inventory_missing')
        _same(matching[0], {'id': actor['id'], 'state': 'active_actor',
                           **{key: actor[key] for key in ('execution_host', 'output', 'process')}},
              'co_resident_actor_inventory_changed')
        total_workers += actor['workers']
    _require(total_workers + contract['supplement_clients'] <= cap,
             'co_resident_sequence_reservation_exceeded')
    resources = contract['execution_resources']
    _require(type(resources) is list and resources
             and len({r['host'] for r in resources}) == len(resources), 'co_resident_resource_hosts')
    resource_hosts = {r['host'] for r in resources}
    _require({a['execution_host'] for a in actors} <= resource_hosts,
             'co_resident_execution_host_unbound')
    for resource in resources:
        for key in ('reserved_actor_workers', 'cpu_per_worker', 'cpu_headroom',
                    'memory_per_worker_bytes', 'memory_headroom_bytes'):
            _integer(resource[key], 1, 'co_resident_resource_reservation:' + key)
        refs = resource['actor_plans']
        _references(refs, 'co_resident_joint_actor_plan_roster')
        _require(all(ref in contract['required_pins'] for ref in refs),
                 'co_resident_joint_plan_not_pinned')
        plans = [json.loads(core.pinned(ref)) for ref in refs]
        _require(all(p['execution_host'] == resource['host'] for p in plans),
                 'co_resident_joint_plan_host_changed')
        for plan in plans:
            _integer(plan['workers'], 1, 'co_resident_joint_plan_workers')
        _require(resource['reserved_actor_workers'] == sum(p['workers'] for p in plans)
                 and all(a['plan'] in refs for a in actors if a['execution_host'] == resource['host']),
                 'co_resident_joint_actor_reservation_too_small')
    client = contract['client_resources']
    _require(type(client['host']) is str and client['host']
             and client['host'] not in resource_hosts, 'co_resident_separate_client_host_required')
    _integer(client['minimum_available_memory_bytes'], 1, 'co_resident_client_memory_floor')
    _integer(client['minimum_free_cpu_count'], 1, 'co_resident_client_cpu_floor')
    return contract


def qualify_observation(value, policy, contract, replay, *, initial=False, at=None):
    """Return the unchanged native observation, or hold new supplement claims.

    Policy/source adoption and observer provenance remain the caller's job. The
    original observation is retained by native run_queue before each claim.
    A hold never changes an actor or cancels an already issued supplement request.
    """
    validate_contract(contract, policy)
    observed = value['co_residency']
    _require(observed['schema'] == OBSERVATION_SCHEMA, 'co_resident_observation_schema')
    maximum = contract['maximum_observation_age_seconds']
    for stamp in ('observed_from', 'observed_until'):
        launch.recent(observed[stamp], maximum, at=at)
    _require(dt.datetime.fromisoformat(observed['observed_until']) >=
             dt.datetime.fromisoformat(observed['observed_from']), 'co_resident_reversed_interval')
    _same([observed['observed_from'], observed['observed_until']],
          [value['observed_from'], value['observed_until']], 'co_resident_native_interval_changed')
    _require(set(observed['checks']) == set(CHECKS)
             and all(observed['checks'][key] is True for key in CHECKS),
             'co_resident_incomplete_live_checks')
    _references(observed['evidence'], 'co_resident_evidence_required')
    _require(all(ref in value['evidence'] for ref in observed['evidence']),
             'co_resident_evidence_not_retained_by_native')
    _same(observed['owner_inventory'], contract['owner_inventory'], 'co_resident_owner_scope_changed')
    provider = observed['provider']
    _same(provider['identity'], contract['provider_identity'], 'co_resident_provider_changed')
    _same(provider['process'], contract['provider_process'], 'co_resident_observed_provider_process')
    _require(provider['host'] == contract['provider_host'], 'co_resident_observed_provider_host')
    _same(provider['server_max_sequences'], contract['server_max_sequences'],
          'co_resident_observed_sequence_cap')
    running = _integer(provider['running_requests'], 0, 'co_resident_running_requests')
    waiting = _integer(provider['waiting_requests'], 0, 'co_resident_waiting_requests')
    preemptions = _integer(provider['preemptions'], 0, 'co_resident_preemptions')
    if waiting or preemptions != contract['baseline_preemptions']:
        raise core.AdmissionHold('co_resident_actor_priority_metrics')
    if running > sum(a['workers'] for a in contract['actors']) + contract['supplement_clients']:
        raise core.AdmissionHold('co_resident_undeclared_request_capacity')
    rows = observed['actors']
    _require([row['id'] for row in rows] == [a['id'] for a in contract['actors']],
             'co_resident_observed_actor_roster')
    for row, actor in zip(rows, contract['actors']):
        _same(row['identity'], actor, 'co_resident_actor_identity_changed')
        _require(row['observed_host'] == actor['execution_host'],
                 'co_resident_actor_observed_on_wrong_host')
        _require(row['process_state'] in ('R', 'S', 'D', 'I') and row['lock_held'] is True,
                 'co_resident_actor_not_healthy_owned')
        guard = row['guard']
        _require(guard['healthy'] is True and set(guard['alarms']) == {
            'owner_identity_invalid', 'reporting_lock_invalid', 'guard_record_invalid',
            'guard_not_ready', 'storage_binding_invalid', 'volume_below_floor'}
            and all(state is False for state in guard['alarms'].values())
            and guard['latest_transition'] is not None, 'co_resident_actor_guard_unhealthy')
        _same(guard['reporter_source'], actor['guard_binding']['reporter_source'],
              'co_resident_actor_guard_source_changed')
        launch.recent(guard['at'], maximum, at=at)
        binding = actor['guard_binding']['binding']
        try:
            free = StorageGuard(binding, lambda unused: guard['fresh_storage']).check()
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise core.IntegrityError('co_resident_actor_guard_storage_invalid') from error
        if free < binding['floor_bytes']:
            raise core.AdmissionHold('co_resident_actor_guard_storage_floor')
        active = _integer(row['active'], 0, 'co_resident_actor_active')
        _require(active <= actor['workers'], 'co_resident_actor_active_above_reservation')
    observed_resources = observed['execution_resources']
    _require([r['host'] for r in observed_resources] ==
             [r['host'] for r in contract['execution_resources']], 'co_resident_resource_scope_changed')
    for row, limits in zip(observed_resources, contract['execution_resources']):
        cpus = _integer(row['cpu_count'], 1, 'co_resident_cpu_count')
        memory = _integer(row['available_memory_bytes'], 0, 'co_resident_available_memory')
        if (cpus < limits['reserved_actor_workers'] * limits['cpu_per_worker'] + limits['cpu_headroom']
                or memory < limits['reserved_actor_workers'] * limits['memory_per_worker_bytes']
                + limits['memory_headroom_bytes']):
            raise core.AdmissionHold('co_resident_actor_resource_headroom')
    client = observed['client_resources']; limits = contract['client_resources']
    _require(client['host'] == limits['host'], 'co_resident_client_host_changed')
    free_cpus = client['free_cpu_count']
    _require(type(free_cpus) in (int, float) and math.isfinite(free_cpus) and free_cpus >= 0,
             'co_resident_client_cpu_measurement')
    memory = _integer(client['available_memory_bytes'], 0, 'co_resident_client_memory_measurement')
    if free_cpus < limits['minimum_free_cpu_count'] or memory < limits['minimum_available_memory_bytes']:
        raise core.AdmissionHold('co_resident_client_resource_headroom')
    launch.qualify_observation(value, policy, replay, initial=initial, at=at)
    return value
