"""Finite, one-client continuations of an already qualified supplement.

The caller retains root authority, durable process/barrier ownership and the
reviewed host observer. These helpers bind exact original ordinal partitions,
occupied pilot attempts and peer owners around the unchanged native queue.
There are no model calls on import, extra draws, capacity promotions or retries.
"""
import functools
import json
import os
from pathlib import Path

from causes import campaign_handoff as handoff
from causes import campaign_supplement as core
from causes import campaign_supplement_co_residency as residency

PARTITION_SCHEMA = 'causes.supplement_finite_partition.v1'
OWNERS_SCHEMA = 'causes.supplement_finite_owners.v1'


def require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def checked(reference):
    return json.loads(core.pinned(reference))


def same(left, right, reason):
    require(core.encoded(left) == core.encoded(right), reason)


def finite_partition(reference):
    """Return exact role jobs and excluded pilot jobs; never choose by outcome."""
    value = checked(reference)
    require(value['schema'] == PARTITION_SCHEMA, 'finite_partition_schema')
    source = checked(value['source_jobs'])
    require(type(source) is list and source, 'finite_source_jobs')
    ids = [job['measurement_id'] for job in source]
    require(len(ids) == len(set(ids)), 'finite_duplicate_measurement')
    require(len({(job['family'], job['model']) for job in source}) == 1,
            'finite_family_model_scope')
    roles = value['roles']
    require(type(roles) is dict and roles, 'finite_roles')
    assigned, excluded, seen, contexts = {}, {}, set(), {}
    for role, part in roles.items():
        core.namespace_component(role)
        require(type(part['max_clients']) is int and part['max_clients'] == 1,
                'finite_one_client_only')
        for field, target in (('ordinals', assigned), ('pilot_ordinals', excluded)):
            indices = part[field]
            require(type(indices) is list and indices and
                    all(type(i) is int and 0 <= i < len(source) for i in indices) and
                    indices == sorted(set(indices)), 'finite_original_order')
            require(not seen.intersection(indices), 'finite_overlapping_ordinals')
            seen.update(indices)
            target[role] = [source[i] for i in indices]
            for job in target[role]:
                context = (job['model'], job['run_id'], job['stage'])
                owner = (role, field)
                require(context not in contexts or contexts[context] == owner,
                        'finite_split_context')
                contexts[context] = owner
        same(checked(part['jobs']), assigned[role], 'finite_job_projection_changed')
        same(checked(part['pilot_jobs']), excluded[role], 'finite_pilot_projection_changed')
    require(seen == set(range(len(source))), 'finite_incomplete_partition')
    require(type(value['remaining_requests']) is int and
            value['remaining_requests'] == sum(map(len, assigned.values())),
            'finite_request_count')
    return value, assigned, excluded


def qualified_pilots(partition, excluded, descriptors):
    """Verify accepted closure custody for every excluded original pilot slot.

    Independent native request/result audits stay in the referenced closure.
    This checks their exact acceptance plus original FINAL identity and status;
    it does not reinterpret labels or rerun a parser.
    """
    require(set(descriptors) == set(excluded), 'finite_pilot_role_roster')
    closures = {}
    for role, jobs in excluded.items():
        descriptor = descriptors[role]
        closure = checked(descriptor['actual'])
        review = checked(descriptor['review'])
        require(review.get(descriptor['acceptance_flag']) is True and
                review.get('findings') == [] and
                review.get('actual') == descriptor['actual'], 'finite_pilot_acceptance')
        order = [job['measurement_id'] for job in jobs]
        require(closure['role'] == role and closure['complete_ids'] == order and
                closure['terminal'] == len(jobs) and closure['active'] == 0 and
                closure['unstarted_ids'] == [] and closure['ambiguous'] == [] and
                closure['all_technical_complete'] is True and
                closure['full_pilot_qualified'] is True, 'finite_pilot_not_complete')
        core.pinned(closure['journal'])
        core.pinned(closure['result'])
        verified = checked(closure['verified_finals'])
        require([row['measurement_id'] for row in verified] == order,
                'finite_verified_pilot_order')
        for job, row in zip(jobs, verified):
            expected = core.measurement_path(closure['output'], job,
                                            family=job['family'], model=job['model']) / 'FINAL.json'
            require(row['final']['path'] == str(expected), 'finite_pilot_final_path')
            final = checked(row['final'])
            require(final['measurement_id'] == job['measurement_id'] and
                    final['input_sha256'] == core.digest(job) and
                    final['technical_status'] == 'complete' and
                    final['integrity_failure'] is False and final['retry'] is False,
                    'finite_pilot_final_changed')
        closures[role] = closure
    return closures


def registered_owners(reference, expected_plans, observed_processes):
    """Reconcile an exact joint registration with a fresh finite-scope census.

    The observer must supply every matching producer, not only declared PIDs.
    Arbitrary unrelated network clients remain outside that census's claim.
    """
    value = checked(reference)
    require(value['schema'] == OWNERS_SCHEMA and value['root_authorized'] is True,
            'finite_owner_registration_authority')
    rows = value['owners']
    require([row['role'] for row in rows] == list(expected_plans), 'finite_owner_roles')
    require(len({(row['host'], row['process']['pid']) for row in rows}) == len(rows),
            'finite_duplicate_owner')
    expected_processes = []
    providers = set()
    for row in rows:
        ref = expected_plans[row['role']]
        same(row['plan'], ref, 'finite_owner_plan')
        plan = checked(ref)
        require(plan['role'] == row['role'] and plan['output_root'] == row['output'] and
                plan['execution_host'] == row['host'] and
                type(row['host']) is str and bool(row['host']) and
                plan['max_clients'] == 1 and type(plan['max_clients']) is int,
                'finite_owner_scope')
        core.canonical(row['output'])
        require(plan['provider_host'] not in providers, 'finite_shared_provider')
        providers.add(plan['provider_host'])
        same(row['source'], plan['source'], 'finite_owner_source')
        core.pinned(row['source'])
        process = row['process']
        require(set(process) == {'pid', 'process_starttime_ticks', 'argv'} and
                type(process['pid']) is int and process['pid'] > 0 and
                type(process['process_starttime_ticks']) is str and
                process['process_starttime_ticks'].isdigit() and
                int(process['process_starttime_ticks']) > 0 and
                type(process['argv']) is list and process['argv'] and
                all(type(arg) is str and '\0' not in arg for arg in process['argv']),
                'finite_owner_process')
        same(process['argv'], row['approved_argv'], 'finite_owner_argv')
        require(plan['source']['path'] in process['argv'] and
                any(arg == ref['path'] or
                    (arg.startswith('{') and _argument_pin(arg) == ref)
                    for arg in process['argv']), 'finite_owner_invocation_binding')
        expected_processes.append({'host': row['host'], 'process': process})
    same(observed_processes, expected_processes, 'finite_unknown_or_changed_owner')
    return expected_processes


def _argument_pin(argument):
    try:
        return json.loads(argument)
    except ValueError:
        return None


def run_finite_queue(partition_reference, role, pilot_descriptors, output,
                     policy, contract, *, worker, observation, source_check,
                     request_check, replay_history, callback_sources, required_pins,
                     pause_paths, capture_reader=None):
    """Run exactly one finite role under the caller's held lock and authority.

    The caller must bind this invocation/partition/role to its exact root
    adoption, IDENTITY and durable start registration before entering. Existing
    native finals and unresolved attempts are reconciled by run_queue, never
    replaced. A host observer must retain fresh joint-owner and actor evidence.
    """
    partition, by_role, excluded = finite_partition(partition_reference)
    require(role in by_role, 'finite_unknown_role')
    closures = qualified_pilots(partition, excluded, pilot_descriptors)
    jobs = by_role[role]
    output = core.canonical(output)
    require(policy['phase'] == 'continue' and policy['max_clients'] == 1 and
            type(policy['max_clients']) is int and policy['output_root'] == str(output),
            'finite_continue_scope')
    require(policy['pilot_measurement_ids'] ==
            [job['measurement_id'] for job in excluded[role]], 'finite_pilot_policy')
    require(policy['local_pause_paths'] == [str(output / 'PAUSE')] and
            policy['local_pause_paths'][0] in pause_paths and pause_paths,
            'finite_pause_scope')
    residency.validate_contract(contract, policy)
    require(handoff.lock_owned_by(output / '.control/executor.lock', os.getpid()),
            'finite_executor_lock_not_owned')
    callbacks = dict(worker=worker, observation=observation, source_check=source_check,
                     request_check=request_check, replay_history=replay_history)
    if capture_reader is not None:
        callbacks['capture_reader'] = capture_reader
    require(set(callback_sources) == set(callbacks), 'finite_callback_roster')
    immutable = [partition_reference, partition['source_jobs'], *required_pins]
    for part in partition['roles'].values():
        immutable.extend([part['jobs'], part['pilot_jobs']])
    for name, closure in closures.items():
        immutable.extend([pilot_descriptors[name]['actual'], pilot_descriptors[name]['review'],
                          closure['journal'], closure['result'], closure['verified_finals']])
        immutable.extend(row['final'] for row in checked(closure['verified_finals']))
    unique = {}
    for ref in immutable:
        require(ref['path'] not in unique or unique[ref['path']] == ref,
                'finite_conflicting_reference')
        unique[ref['path']] = ref
    payload_identity = core.encoded([jobs, policy, contract])

    def sources():
        require(core.encoded([jobs, policy, contract]) == payload_identity,
                'finite_live_binding_changed')
        for ref in unique.values():
            core.pinned(ref)
        for name, callback in callbacks.items():
            require(core.pin(Path(callback.__code__.co_filename).resolve()) ==
                    callback_sources[name], 'finite_callback_source:' + name)
        source_check()

    observations = 0
    cached_replay = functools.lru_cache(maxsize=16)(replay_history)

    def admit():
        nonlocal observations
        try:
            sources()
            value = observation()
            observations += 1
            return residency.qualify_observation(value, policy, contract,
                                                  cached_replay,
                                                  initial=observations == 1,
                                                  capture_reader=capture_reader)
        except Exception as error:
            raise core.AdmissionHold(str(error)) from error

    def prepare(job):
        sources()
        return request_check(job)

    def execute(job, destination):
        sources()
        return worker(job, destination)

    sources()
    # Replay sealed histories before collecting time-sensitive admission evidence.
    for donor in policy['donors']:
        cached_replay(core.pinned(donor['baseline']))
    return core.run_queue(output, jobs, execute, admit, prepare, pause_paths,
                          family=jobs[0]['family'], model=jobs[0]['model'],
                          initial_failures=policy['initial_failures'], phase='continue',
                          max_clients=1,
                          local_free_space_floor_bytes=policy['local_free_space_floor_bytes'])
